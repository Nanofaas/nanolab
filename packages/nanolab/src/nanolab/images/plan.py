"""Expand the live repository into the image build cells a release ships.

The plan is derived from the checkout rather than a static manifest: the
targets are the control plane, the watchdog, and every function that has an
example directory. Each target is then crossed with the requested
architectures and flavors into cells, which the Bake renderer and the build
runners consume without restating any of this structure themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nanolab.functions.catalog import FunctionDefinition, list_functions
from nanolab.images.control_plane_variants import VARIANTS_BY_KEY, jvm_optimization
from nanolab.release.versioning import normalize_version
from nanolab.tasks.deployment import LOCAL_REGISTRY

ImageArchitecture = Literal["amd64", "arm64"]
ImageFlavor = Literal["jvm", "native", "default"]

DEFAULT_ARCHITECTURES: tuple[ImageArchitecture, ...] = ("amd64", "arm64")
DEFAULT_REGISTRY = f"{LOCAL_REGISTRY}/nanofaas"

NATIVE_JAVA_DOCKERFILE = Path("deploy/native-java/Dockerfile")
JVM_RELEASE_PROFILE = VARIANTS_BY_KEY["jvm-g1-c2"]
NATIVE_RELEASE_PROFILE = VARIANTS_BY_KEY["native-o3-g1"]


@dataclass(frozen=True)
class NativeBuild:
    """How nanoFaaS builds one Java native image since commit `c3179fbb`.

    These are the three build args `scripts/native-java-image.sh` feeds to the
    shared Dockerfile. nanolab bakes them rather than shelling out to the
    script, so native cells stay inside the single buildx graph the release
    already digest-pins and verifies.
    """

    task: str
    binary: Path
    gradle_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageTarget:
    """One image a release builds, and the sources it builds from.

    A target is architecture- and flavor-agnostic; the cells crossed from it
    carry the resolved tag and image reference for a single build.
    """

    name: str
    flavors: tuple[ImageFlavor, ...]
    dockerfile: Path
    context: Path
    native_build: NativeBuild | None = None
    jvm_prerequisite_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageCell:
    """One buildable combination of a target, an architecture and a flavor.

    The tag and image reference are resolved at expansion time, so every
    consumer of a cell builds and tags the exact same thing.
    """

    target: ImageTarget
    architecture: ImageArchitecture
    flavor: ImageFlavor
    tag: str
    image: str

    @property
    def platform(self) -> str:
        """The Docker platform string this cell targets, such as `linux/amd64`."""
        return f"linux/{self.architecture}"

    @property
    def native_build(self) -> NativeBuild | None:
        """The native build contract this cell compiles against, or None.

        Only native-flavored cells of a target that declares a native build
        carry one; every other cell returns None.
        """
        if self.flavor != "native":
            return None
        return self.target.native_build

    @property
    def dockerfile(self) -> Path:
        """The Dockerfile that builds this cell.

        Native cells use the shared GraalVM Dockerfile, since their target
        carries a native build; every other cell uses its target's own.
        """
        native = self.native_build
        return NATIVE_JAVA_DOCKERFILE if native is not None else self.target.dockerfile

    @property
    def context(self) -> Path:
        """The build context this cell's Dockerfile is resolved against.

        Native cells build from the repository root; every other cell builds
        from its target's own directory.
        """
        # The shared native Dockerfile does `COPY . .` — it only builds from the
        # repository root, never from the target's own directory.
        native = self.native_build
        return Path() if native is not None else self.target.context

    @property
    def build_args(self) -> dict[str, str]:
        """The build arguments this cell needs, keyed by argument name.

        JVM cells carry the release tuning profile, native cells the
        compilation contract the shared Dockerfile reads, and cells of any
        other flavor none at all.
        """
        if self.flavor == "jvm":
            return {"JVM_TUNING": JVM_RELEASE_PROFILE.build_env["JVM_TUNING"]}
        native = self.native_build
        if native is None:
            return {}
        profile = NATIVE_RELEASE_PROFILE.build_env
        # Only the control plane's own build carries its identity into the
        # binary; function images are held fixed across variants and must not
        # pick up control-plane metadata (see control_plane_variants module doc).
        identity_args = (
            (
                "-PnanofaasBuildType=native",
                f"-PnanofaasBuildVariant={NATIVE_RELEASE_PROFILE.key}",
                f"-PnanofaasBuildOptimization={profile['NATIVE_OPTIMIZATION']}",
            )
            if self.target.name == "control-plane"
            else ()
        )
        return {
            "NATIVE_TASK": native.task,
            "NATIVE_BINARY": native.binary.as_posix(),
            "GRADLE_ARGS": " ".join(
                (
                    *native.gradle_args,
                    f"-PnativeOptimization={profile['NATIVE_OPTIMIZATION']}",
                    f"-PnativeGc={profile['NATIVE_GC']}",
                    *identity_args,
                )
            ),
            "GRAALVM_DISTRIBUTION": "oracle",
        }

    @property
    def prerequisite_command(self) -> tuple[str, ...] | None:
        """The command that must finish before this cell's image build, or None.

        JVM cells boot-jar the target first so the Dockerfile has an artifact
        to copy; native and default cells have no such prerequisite.
        """
        if self.flavor != "jvm":
            return None
        identity_args = (
            (
                "-PnanofaasBuildType=jvm",
                f"-PnanofaasBuildVariant={JVM_RELEASE_PROFILE.key}",
                f"-PnanofaasBuildOptimization={jvm_optimization(JVM_RELEASE_PROFILE)}",
            )
            if self.target.name == "control-plane"
            else ()
        )
        return ("./gradlew", *self.target.jvm_prerequisite_arguments, *identity_args)


@dataclass(frozen=True)
class ImagePlan:
    """Every target a release builds, and the cells expanded from them."""

    version: str
    registry: str
    targets: tuple[ImageTarget, ...]
    cells: tuple[ImageCell, ...]

    @property
    def target_names(self) -> frozenset[str]:
        """The names of all targets in the plan, for validating selectors."""
        return frozenset(target.name for target in self.targets)


def build_image_plan(
    repo_root: Path,
    version: str,
    *,
    registry: str = DEFAULT_REGISTRY,
    selectors: Sequence[str] = (),
    architectures: Sequence[ImageArchitecture] = DEFAULT_ARCHITECTURES,
    flavors: Sequence[ImageFlavor] = ("jvm", "native", "default"),
) -> ImagePlan:
    """Expand the live repository catalog into immutable image build cells."""
    repo_root = Path(repo_root).resolve()
    _, version_tag = normalize_version(version)
    registry = registry.rstrip("/")
    if not registry:
        raise ValueError("image registry must not be empty")

    targets = _select_targets(_all_targets(repo_root), selectors)
    selected_flavors = frozenset(flavors)
    cells = tuple(
        _cell(target, architecture, flavor, version_tag, registry)
        for architecture in architectures
        for target in targets
        for flavor in target.flavors
        if flavor in selected_flavors
    )
    return ImagePlan(
        version=version_tag, registry=registry, targets=targets, cells=cells
    )


def _all_targets(repo_root: Path) -> tuple[ImageTarget, ...]:
    targets = [
        ImageTarget(
            name="control-plane",
            flavors=("jvm", "native"),
            dockerfile=Path("platform/control-plane/Dockerfile"),
            context=Path("platform/control-plane"),
            native_build=NativeBuild(
                task=":control-plane:nativeCompile",
                binary=Path(
                    "platform/control-plane/build/native/nativeCompile/control-plane"
                ),
                gradle_args=("-PcontrolPlaneModules=all",),
            ),
            jvm_prerequisite_arguments=(
                ":control-plane:bootJar",
                "-PcontrolPlaneModules=all",
            ),
        ),
        ImageTarget(
            name="java-warm-echo",
            flavors=("jvm", "native"),
            dockerfile=Path("services/java/warm-echo/Dockerfile"),
            context=Path("services/java/warm-echo"),
            native_build=NativeBuild(
                task=":services:java:warm-echo:nativeCompile",
                binary=Path(
                    "services/java/warm-echo/build/native/nativeCompile/warm-echo"
                ),
            ),
            jvm_prerequisite_arguments=(":services:java:warm-echo:bootJar",),
        ),
        ImageTarget(
            name="watchdog",
            flavors=("default",),
            dockerfile=Path("runtimes/watchdog/Dockerfile"),
            context=Path("runtimes/watchdog"),
        ),
        *(
            _function_target(repo_root, function)
            for function in list_functions(repo_root)
            if function.example_dir is not None
        ),
    ]
    targets.sort(key=lambda target: target.name)
    _validate_targets(repo_root, targets)
    return tuple(targets)


def _function_target(repo_root: Path, function: FunctionDefinition) -> ImageTarget:
    if function.example_dir is None:
        raise ValueError(f"function has no source directory: {function.key}")
    source_dir = function.example_dir.resolve().relative_to(repo_root)
    prefix = {"exec": "bash", "java-lite": "java-lite"}.get(
        function.runtime, function.runtime
    )
    name = f"{prefix}-{function.family}"

    if function.runtime == "java":
        return ImageTarget(
            name=name,
            flavors=("jvm", "native"),
            dockerfile=source_dir / "Dockerfile",
            context=source_dir,
            native_build=NativeBuild(
                task=f":functions:java:{function.family}:nativeCompile",
                binary=Path(
                    f"functions/java/{function.family}"
                    f"/build/native/nativeCompile/{function.family}"
                ),
            ),
            jvm_prerequisite_arguments=(f":functions:java:{function.family}:bootJar",),
        )
    return ImageTarget(
        name=name,
        flavors=("native",) if function.runtime == "java-lite" else ("default",),
        dockerfile=source_dir / "Dockerfile",
        context=Path(),
    )


def _validate_targets(repo_root: Path, targets: Sequence[ImageTarget]) -> None:
    names = [target.name for target in targets]
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if duplicates:
        raise ValueError(f"duplicate image target: {', '.join(duplicates)}")
    required = {target.dockerfile for target in targets}
    required.update(
        NATIVE_JAVA_DOCKERFILE for target in targets if target.native_build is not None
    )
    missing = sorted(
        path.as_posix() for path in required if not (repo_root / path).is_file()
    )
    if missing:
        raise FileNotFoundError(f"missing image Dockerfile: {', '.join(missing)}")


def _select_targets(
    targets: tuple[ImageTarget, ...], selectors: Sequence[str]
) -> tuple[ImageTarget, ...]:
    if not selectors:
        return targets
    requested = frozenset(selectors)
    known = {target.name for target in targets}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError(f"unknown image target: {', '.join(unknown)}")
    return tuple(target for target in targets if target.name in requested)


def _cell(
    target: ImageTarget,
    architecture: ImageArchitecture,
    flavor: ImageFlavor,
    version_tag: str,
    registry: str,
) -> ImageCell:
    tag = (
        f"{version_tag}-{architecture}"
        if flavor == "default"
        else f"{version_tag}-{architecture}-{flavor}"
    )
    return ImageCell(
        target=target,
        architecture=architecture,
        flavor=flavor,
        tag=tag,
        image=f"{registry}/{target.name}:{tag}",
    )

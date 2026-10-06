"""How a scenario's function key becomes something a task can run.

Four plan builders need this, which is why it is a module of its own: it used
to live in `validate` under private names that everyone imported anyway, so the
underscore documented an intent the code contradicted.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.catalog import FunctionDefinition, resolve_function_definition
from nanolab.tasks.validate import ValidateFunction as SonataFunction
from nanolab.workspace.paths import discover_tool_root


@dataclass(frozen=True, slots=True)
class ResolvedFunction:
    """One function key resolved into the concrete values a task runs with.

    The identity (`key`, `name`, `image`), what to build the image from
    (`build_argv`, `image_build_argv`), the serialized `payload` to send, and
    the resource and scaling knobs the scenario declared.
    """

    key: str
    name: str
    image: str
    build_argv: tuple[str, ...]
    payload: str
    image_build_argv: tuple[str, ...] | None = None
    resources: dict[str, object] | None = None
    scaling_config: dict[str, object] | None = None
    timeout_ms: int = 5000
    concurrency: int = 2
    queue_size: int = 20
    max_retries: int = 3


@dataclass(frozen=True, slots=True)
class FunctionPayload:
    """One payload file a function owns: raw input and the expected output."""

    name: str
    input: object
    expected: object


def resolve_function_payloads(
    key: str, source_root: Path | None = None
) -> tuple[FunctionPayload, ...]:
    """Return the payloads a function owns under its `payloads/` directory.

    Each file lives at `functions/<runtime>/<family>/payloads/` and has the
    nanoFaaS function-test shape `{description, input, expected}`. Functions
    without a payload directory (or none at all) contribute nothing.
    """
    definition = resolve_function_definition(key, source_root)
    if definition.example_dir is None:
        return ()
    payload_dir = definition.example_dir / "payloads"
    if not payload_dir.is_dir():
        return ()
    payloads: list[FunctionPayload] = []
    for path in sorted(payload_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        payloads.append(
            FunctionPayload(
                name=path.stem,
                input=data.get("input"),
                expected=data.get("expected"),
            )
        )
    return tuple(payloads)


def _function_image(definition: FunctionDefinition) -> str:
    if definition.default_image is None:
        raise ValueError(f"function {definition.key!r} has no image")
    return definition.default_image


def _build_argv(definition: FunctionDefinition, image: str) -> tuple[str, ...]:
    family = definition.family
    runtime = definition.runtime
    if runtime == "java":
        argv = (
            "./gradlew",
            f":functions:java:{family}:bootJar",
            "--quiet",
        )
    else:
        runtime_dir = {"java-lite": "java", "exec": "bash"}.get(runtime, runtime)
        suffix = "-lite" if runtime == "java-lite" else ""
        argv = (
            "docker",
            "build",
            "-t",
            image,
            "-f",
            f"functions/{runtime_dir}/{family}{suffix}/Dockerfile",
            ".",
        )
    return argv


def _image_build_argv(
    definition: FunctionDefinition, image: str
) -> tuple[str, ...] | None:
    if definition.runtime != "java":
        return None
    family = definition.family
    return (
        "docker",
        "build",
        "-t",
        image,
        "-f",
        f"functions/java/{family}/Dockerfile",
        f"functions/java/{family}",
    )


def _function_name(definition: FunctionDefinition) -> str:
    if definition.example_dir is None:
        return definition.key
    manifest = yaml.safe_load(
        (definition.example_dir / "function.yaml").read_text(encoding="utf-8")
    )
    return str(manifest.get("name", definition.key))


def _payload(definition: FunctionDefinition, tool_root: Path | None = None) -> str:
    if definition.default_payload_file is None:
        return '{"input":{}}'
    product_root = tool_root or discover_tool_root()
    payload_path = (
        product_root / "scenarios" / "payloads" / definition.default_payload_file
    )
    if not payload_path.exists():
        return '{"input":{}}'
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return json.dumps({"input": payload}, separators=(",", ":"))


def resolve_recipe_services(
    recipe: Path, *, source_root: Path, tool_root: Path | None = None
) -> tuple[dict[str, SonataFunction], tuple[str, ...]]:
    """Resolve managed Java services and the standalone watchdog artifact."""
    profile = yaml.safe_load(recipe.read_text(encoding="utf-8"))
    services: dict[str, SonataFunction] = {}
    registration_names: set[str] = set()
    component_names: set[str] = set()
    standalone: list[str] = []
    for entry in profile.get("services", []):
        name = entry.get("name")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
        ):
            raise ValueError("Recipe service needs a simple component name")
        if name in component_names:
            raise ValueError(
                "Recipe services need unique registration and component names"
            )
        component_names.add(name)
        if (name, entry.get("sdk")) == ("watchdog", "dockerfile"):
            if not (source_root / "runtimes/watchdog/Dockerfile").is_file():
                raise ValueError("Recipe watchdog lacks Dockerfile")
            standalone.append(name)
            continue
        if entry.get("sdk") != "java":
            raise ValueError(
                f"Recipe service {name} needs the Java invocation contract"
            )
        manifest_path = source_root / "services/java" / name / "function.yaml"
        if not manifest_path.is_file():
            raise ValueError(f"Recipe service {name} lacks function.yaml")
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("executionMode") != "DEPLOYMENT":
            raise ValueError(f"Recipe service {name} must use DEPLOYMENT")
        definition = FunctionDefinition(
            key=f"{name}-java",
            family=name,
            runtime="java",
            description="Recipe service",
            example_dir=manifest_path.parent,
            default_image=manifest.get("image"),
            default_payload_file=manifest.get("catalog", {}).get("defaultPayload"),
        )
        registration = _function_name(definition)
        if not registration or registration in registration_names or name in services:
            raise ValueError(
                "Recipe services need unique registration and component names"
            )
        registration_names.add(registration)
        image = _function_image(definition)
        services[name] = SonataFunction(
            name=registration,
            image=image,
            payload=_payload(definition, tool_root),
            build_argv=("./gradlew", f":services:java:{name}:bootJar", "--quiet"),
        )
    return services, tuple(standalone)


def resolve_function(
    config: ScenarioConfig,
    key: str,
    *,
    source_root: Path | None = None,
    tool_root: Path | None = None,
) -> ResolvedFunction:
    """Resolve a scenario function key into the values a task runs with.

    The image is taken from the function definition and must be present; the
    name and payload fall back when the definition does not supply one, and
    the resource overrides come from the scenario's `resources` map.
    """
    definition = resolve_function_definition(key, source_root)
    image = _function_image(definition)
    resource = config.resources.get(key)
    return ResolvedFunction(
        key=key,
        name=_function_name(definition),
        image=image,
        build_argv=_build_argv(definition, image),
        image_build_argv=_image_build_argv(definition, image),
        payload=_payload(definition, tool_root),
        resources=(
            resource.model_dump(by_alias=True, exclude_none=True)
            if resource is not None
            else None
        ),
    )


def sonata_function(resolved: ResolvedFunction) -> SonataFunction:
    """Convert the product's resolved function to Sonata's public task shape."""
    return SonataFunction(
        name=resolved.name,
        image=resolved.image,
        payload=resolved.payload,
        build_argv=resolved.build_argv,
        image_build_argv=resolved.image_build_argv,
        resources=resolved.resources,
        scaling_config=resolved.scaling_config,
        timeout_ms=resolved.timeout_ms,
        concurrency=resolved.concurrency,
        queue_size=resolved.queue_size,
        max_retries=resolved.max_retries,
    )

"""Sonata build execution against an immutable snapshot with observed receipts."""

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from sonata_engine import Resource, Steps, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions

from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.images import BuildReceipt, BuildRecipe, freeze_build_receipt
from nanolab.tasks.soak.sources import (
    SourceSnapshot,
    materialize_snapshot,
    verify_snapshot,
)


@dataclass(frozen=True)
class ObservedBuild:
    """Collected effective identities; command arguments are not provenance."""

    digest: str
    toolchains: Mapping[str, str]
    base_images: Mapping[str, str]
    logs: tuple[Path, ...]


class BuildImagesTask(Task[tuple[BuildReceipt, ...]]):
    """Publish all recipes, requiring a collector before freezing each output.

    Build-stage instrumentation observes actual compiler/version commands before
    the collector validates published provenance. The executor must expose its
    last owned result/log and a read-only ``observe`` method, and must stop and
    reap owned commands on cancellation.
    """

    title = "Build publish and freeze all soak images"
    idempotent = False

    def __init__(
        self,
        recipes: tuple[BuildRecipe, ...],
        *,
        snapshot: Resource[SourceSnapshot],
        executor: CommandTaskExecutor,
        output_dir: Path,
        collect: Callable[[BuildRecipe, Path, Path], ObservedBuild],
        artifact_limit_bytes: int,
    ) -> None:
        """Bind immutable recipes, an owned snapshot and observed provenance."""
        if not recipes or len({recipe.role for recipe in recipes}) != len(recipes):
            raise ValueError("one build recipe is required per role")
        if any(recipe.mode != "build" or recipe.bake is None for recipe in recipes):
            raise ValueError(
                "build execution requires build recipes; "
                "prebuilt needs receipt validation"
            )
        self.recipes = recipes
        self.snapshot = snapshot
        self.executor = executor
        self.output_dir = output_dir
        self.collect = collect
        self.artifact_limit_bytes = artifact_limit_bytes

    def run(self, inputs: TaskInputs) -> TaskOutcome[tuple[BuildReceipt, ...]]:
        """Build and freeze every role, preserving partial evidence on failure."""
        from nanolab.tasks.soak.build_observation import BuildObservationCapture

        snapshot = inputs.resource(self.snapshot)
        writer = ArtifactWriter(self.output_dir, self.artifact_limit_bytes)
        receipts = []
        capture = None
        try:
            for index, recipe in enumerate(self.recipes):
                work = materialize_snapshot(
                    snapshot, self.output_dir / f"workspace-{index}"
                )
                bake = self.output_dir / f"bake-{index}.json"
                metadata = self.output_dir / f"metadata-{index}.json"
                capture = BuildObservationCapture(
                    recipe,
                    metadata,
                    work,
                    source_fingerprint=snapshot.fingerprint,
                    artifact_limit_bytes=self.artifact_limit_bytes,
                )
                recipe = capture.prepare()
                bake.write_text(json.dumps(recipe.bake), encoding="utf-8")
                build_argv = (
                    "docker",
                    "buildx",
                    "bake",
                    "-f",
                    str(bake.absolute()),
                    "--push",
                    "--provenance=mode=max",
                    "--metadata-file",
                    str(metadata.absolute()),
                    "--progress=plain",
                )
                capture.start(build_argv)
                writer.append(
                    "build-events", {"role": recipe.role, "status": "started"}
                )

                # Bound explicitly: the closure is only ever called inside
                # this iteration, but the defaults keep that true by
                # construction rather than by reading the call sites.
                def execute(
                    kind, argv, recipe=recipe, work=work, capture=capture, **extra
                ):
                    previous_log = getattr(self.executor, "last_log_path", None)
                    try:
                        Steps(
                            title=f"Observe {kind} {recipe.role}",
                            steps=(
                                CommandTask(
                                    title=f"{kind} {recipe.role}",
                                    argv=argv,
                                    executor=self.executor,
                                    role="host",
                                    options=CommandOptions(
                                        cwd=work,
                                        env={"BUILDX_METADATA_PROVENANCE": "max"},
                                    ),
                                ),
                            ),
                        ).run(inputs)
                    except BaseException as error:
                        if (
                            getattr(self.executor, "last_log_path", None)
                            != previous_log
                        ):
                            try:
                                capture.record(kind, argv, self.executor, **extra)
                            except BaseException as capture_error:
                                error.add_note(f"command observation: {capture_error}")
                        raise
                    if getattr(self.executor, "last_log_path", None) == previous_log:
                        raise ValueError(
                            "owned executor did not produce a fresh command log"
                        )
                    capture.record(kind, argv, self.executor, **extra)

                if recipe.prerequisite_argv is not None:
                    execute("prerequisite", recipe.prerequisite_argv)
                    capture.capture_toolchains("prerequisite")
                    capture.require_toolchains({"java", "gradle"})
                execute("build", build_argv)
                observe = getattr(self.executor, "observe", None)
                if not callable(observe):
                    raise TypeError("build executor must provide an observe callback")
                capture.complete(execute, cast(Callable[..., bytes], observe))
                observed = self.collect(recipe, metadata, work)
                receipt = replace(
                    freeze_build_receipt(
                        recipe,
                        snapshot.fingerprint,
                        observed.digest,
                        toolchains=observed.toolchains,
                        base_images=observed.base_images,
                        logs=tuple(
                            dict.fromkeys((*observed.logs, *capture.evidence_paths))
                        ),
                    ),
                    original_recipe_fingerprint=capture.original.recipe_fingerprint,
                )
                writer.write_json(
                    f"build-{index}.json",
                    {"schema": "nanolab-soak-v1", **asdict(receipt)},
                )
                receipts.append(receipt)
            verify_snapshot(snapshot)
            writer.write_json(
                "frozen-images.json",
                {
                    "schema": "nanolab-soak-v1",
                    "source_fingerprint": snapshot.fingerprint,
                    "images": {item.role: item.image_digest for item in receipts},
                },
            )
            return TaskOutcome(value=tuple(receipts))
        except BaseException as error:
            if capture is not None:
                try:
                    capture.fail(error)
                except BaseException as capture_error:
                    error.add_note(f"partial build observation failed: {capture_error}")
            try:
                writer.write_json(
                    "terminal.json",
                    {
                        "schema": "nanolab-soak-v1",
                        "status": "INCONCLUSIVE"
                        if isinstance(error, Exception)
                        else "ABORTED",
                        "reason": str(error)[:1024],
                        "completed_roles": [item.role for item in receipts],
                    },
                )
            except BaseException as report_error:
                error.add_note(f"build terminal receipt failed: {report_error}")
            raise
        finally:
            writer.close()

"""Everything the matrix builds once, before any cell runs.

A cell measures a control-plane build, so nothing a cell does may build anything:
`platform.py` gates the control-plane image and the function images behind the
same `build_images` flag, and a run that rebuilt its own artefacts could not
promise that cell 1 and cell 12 ran the same function.

So the images are compiled here, once, and every cell is handed pinned tags. The
functions in particular are built once on purpose — they are held fixed across
the whole matrix, and rebuilding them twelve times would let base-image drift or
a dependency resolved on a different day become a difference between variants.

Everything runs on the VM. Native images are compiled for the machine that runs
them, and a build made on an arm64 laptop is not the artefact under measurement.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from sonata_engine import Workflow
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.comparison.manifest import ComparisonManifest
from nanolab.comparison.profiles import PreparedComparison
from nanolab.tasks.components.operations import RemoteCommandOperation
from nanolab.tasks.recipes.remote import bundle_recipe_source, remote_recipe_root
from nanolab.tasks.vm.models import VmRequest
from nanolab.workspace.recipe import RecipeRun, prepare_recipe_run

# The control-plane API on the VM. The matrix reuses one cluster for every cell,
# so a run that was interrupted leaves whatever the interrupted cell had already
# registered — and the next run dies on its first cell with a 409, having done
# nothing wrong.
CONTROL_PLANE_NODE_PORT = 30080


def leftover_cleanup_operations(  # NOSONAR (S8495): one operation per function
    function_names: Sequence[str],
    *,
    node_port: int = CONTROL_PLANE_NODE_PORT,
) -> tuple[RemoteCommandOperation, ...]:
    """Remove anything a previous, interrupted matrix left registered.

    Deliberately tolerant of every failure it can meet: the control plane may not
    be deployed yet on a fresh VM, and the function is usually absent, which is
    the point. Both are ordinary, so neither may stop a run — the only thing this
    must not do is leave a 409 waiting for the first cell.

    Not a substitute for the workflow's own compensation, which deregisters on a
    clean exit. This is for the exit that was not clean.
    """
    if not function_names:
        return ()
    deletes = " ; ".join(
        f"curl -s -o /dev/null -m 5 -X DELETE "
        f"http://127.0.0.1:{node_port}/v1/functions/{name} || true"
        for name in function_names
    )
    return (
        RemoteCommandOperation(
            operation_id="prepare.cleanup.leftover_functions",
            summary="Deregister functions left by an interrupted run",
            argv=("sh", "-c", deletes),
            execution_target="vm",
        ),
    )


@dataclass(frozen=True)
class ComparisonStage:
    """One captured source, per-variant profiles and the owned remote directory."""

    source: Path
    remote_root: PurePosixPath
    runs: Mapping[str, RecipeRun]


# The same probe runs on the captured checkout and the VM, comparing tracked
# content and executable bits independently of Git's revision/dirty flags.
SOURCE_PROBE = """
import hashlib, json, os, subprocess
from pathlib import Path
def git(*args):
    return subprocess.check_output(('git', *args))
paths = set(git('ls-files', '-z').split(b'\\0'))
paths.update(git('ls-tree', '-r', '--name-only', '-z', 'HEAD').split(b'\\0'))
files = {}
for raw in sorted(paths - {b''}):
    name = os.fsdecode(raw)
    path = Path(name)
    if path.is_symlink():
        files[name] = {'symlink': str(path.readlink())}
    elif path.is_file():
        files[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'executable': bool(path.stat().st_mode & 0o111)}
    else:
        files[name] = {'absent': True}
print(json.dumps({'revision': git('rev-parse', 'HEAD').decode().strip(),
    'patchSha256': hashlib.sha256(
        git('diff', 'HEAD', '--binary', '--no-ext-diff')).hexdigest(),
    'files': files}, sort_keys=True))
"""


def captured_source_state(source: Path) -> dict[str, object]:
    """Inspect every tracked input in a captured source checkout."""
    return json.loads(
        subprocess.check_output(("python3", "-c", SOURCE_PROBE), cwd=source)
    )


def comparison_remote_command(
    provider: VmCommandProvider,
    request: VmRequest,
    argv: tuple[str, ...],
    *,
    remote_dir: str | None = None,
) -> str:
    """Execute one VM command and fail closed on unsuccessful execution."""
    result = provider.exec_argv(
        request, argv, env=None, remote_dir=remote_dir, dry_run=False
    )
    if result.return_code:
        raise RuntimeError(f"Comparison VM command failed: {argv[0]}: {result.stderr}")
    return result.stdout


def verify_comparison_source(
    stage: ComparisonStage, provider: VmCommandProvider, request: VmRequest
) -> dict[str, object]:
    """Require identical captured and remote tracked source before publication."""
    expected = captured_source_state(stage.source)
    actual = json.loads(
        comparison_remote_command(
            provider,
            request,
            ("python3", "-c", SOURCE_PROBE),
            remote_dir=str(stage.remote_root / "source"),
        )
    )
    if actual != expected:
        raise ValueError("Comparison staged source differs from captured inputs")
    return actual


def stage_comparison(
    *,
    source: Path,
    profiles: Mapping[str, Path],
    root: Path,
    tag: str,
    provider: VmCommandProvider,
    request: VmRequest,
) -> ComparisonStage:
    """Capture and upload one source checkout shared by every comparison profile."""
    if not profiles or next(iter(profiles)) != "jvm":
        raise ValueError("Comparison staging requires the shared JVM profile first")
    captured = prepare_recipe_run(source, profiles["jvm"], root / "inputs", tag)
    remote_root = remote_recipe_root(request, tag)
    runs = {}
    profile_root = root / "profiles"
    profile_root.mkdir(exist_ok=True)
    for key, profile in profiles.items():
        local_profile = profile_root / f"{key}.yaml"
        content = profile.read_bytes()
        if local_profile.exists() and local_profile.read_bytes() != content:
            raise ValueError("Comparison staged profile differs from captured inputs")
        local_profile.write_bytes(content)
        runs[key] = RecipeRun(
            captured.source_dir,
            local_profile,
            root / "prepare" / key / "distribution",
            tag,
        )
    stage = ComparisonStage(captured.source_dir, remote_root, runs)
    if captured_source_state(source) != captured_source_state(stage.source):
        raise ValueError("Comparison source changed during capture")
    archive = root / "prepare" / "source.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    bundle_recipe_source(stage.source, archive)
    comparison_remote_command(
        provider,
        request,
        (
            "mkdir",
            "-p",
            str(remote_root / "profiles"),
            str(remote_root / "distributions"),
        ),
    )
    transfers = [
        (archive, remote_root / "source.tar.gz"),
        *(
            (run.recipe, remote_root / "profiles" / f"{key}.yaml")
            for key, run in runs.items()
        ),
    ]
    for local, remote in transfers:
        result = provider.transfer_to(request, source=local, destination=str(remote))
        if result.return_code:
            raise RuntimeError("Comparison VM staging transfer failed")
        expected = hashlib.sha256(local.read_bytes()).hexdigest()
        actual = comparison_remote_command(
            provider, request, ("sha256sum", str(remote))
        ).split()
        if not actual or actual[0] != expected:
            raise ValueError("Comparison staged archive/profile hash differs")
    comparison_remote_command(
        provider,
        request,
        ("tar", "-xzf", str(remote_root / "source.tar.gz"), "-C", str(remote_root)),
    )
    evidence = verify_comparison_source(stage, provider, request)
    (root / "prepare" / "source.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return stage


def prepare_comparison(
    *,
    stage: ComparisonStage,
    manifest: ComparisonManifest,
    root: Path,
    provider: VmCommandProvider,
    request: VmRequest,
) -> PreparedComparison:
    """Publish missing recipes in order and commit only verified receipts."""
    import contextlib

    from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
    from sonata_tasks.vm.logged import run_remote_logged

    from nanolab.comparison.evidence import (
        require_recorded_publications,
        verify_comparison_publication,
    )
    from nanolab.comparison.manifest import write_comparison_manifest
    from nanolab.comparison.profiles import declared_options
    from nanolab.tasks.recipes.workflow import (
        RecipeDistribution,
        read_distribution,
        recipe_command,
    )
    from nanolab.tasks.vm.runners import VmFileFetcher

    require_recorded_publications(
        manifest=manifest, provider=provider, request=request, root=root
    )
    identity = cast(dict[str, Any], manifest.identity)
    distributions: dict[str, RecipeDistribution] = {}
    resources: list[Resource[RecipeDistribution]] = []
    for variant, run in stage.runs.items():

        def acquire(
            _inputs: TaskInputs, key: str = variant, local: RecipeRun = run
        ) -> RecipeDistribution:
            if key in manifest.publications:
                receipt = cast(dict[str, Any], manifest.publications[key])
                result = read_distribution(
                    root / receipt["distribution"]["path"],
                    recipe=local.recipe,
                    tag=local.tag,
                    published=True,
                )
                distributions[key] = result
                return result
            evidence_dir = root / "prepare" / key
            evidence_dir.mkdir(parents=True, exist_ok=True)
            state = verify_comparison_source(stage, provider, request)
            source_identity = identity["nanofaas"]
            if (
                state["revision"] != source_identity["revision"]
                or state["patchSha256"] != source_identity["patchSha256"]
            ):
                raise ValueError("Comparison staged source differs before publication")
            (evidence_dir / "source-before-publication.json").write_text(
                json.dumps(state, indent=2) + "\n"
            )
            remote_profile = stage.remote_root / "profiles" / f"{key}.yaml"
            profile_hash = comparison_remote_command(
                provider, request, ("sha256sum", str(remote_profile))
            ).split()
            if (
                not profile_hash
                or profile_hash[0] != identity["profiles"][key]["sha256"]
            ):
                raise ValueError("Comparison staged profile differs before publication")
            remote_output = stage.remote_root / "distributions" / key
            comparison_remote_command(
                provider, request, ("mkdir", "-p", str(remote_output))
            )
            comparison_remote_command(
                provider,
                request,
                ("rm", "-f", str(remote_output / "distribution.json")),
            )
            report = local.output_dir / "distribution.json"
            report.unlink(missing_ok=True)
            native = declared_options(local.recipe)["mode"] == "native"
            properties = identity["nativeProperties"]
            command = recipe_command(
                "publishRecipe",
                recipe=str(remote_profile),
                output=str(remote_output),
                tag=local.tag,
                native_build_memory=properties["buildMemory"] if native else None,
                native_parallelism=properties["parallelism"] if native else None,
            )
            fetcher = VmFileFetcher(provider, request)
            try:
                run_remote_logged(
                    provider,
                    request,
                    (command,),
                    remote_dir=stage.remote_root / "source",
                    remote_log=stage.remote_root / f"{key}.gradle.log",
                    local_log=evidence_dir / "gradle.log",
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    fetcher.fetch_from(str(remote_output / "distribution.json"), report)
                raise
            fetcher.fetch_from(str(remote_output / "distribution.json"), report)
            result = read_distribution(
                report, recipe=local.recipe, tag=local.tag, published=True
            )
            receipt = verify_comparison_publication(
                distribution=result,
                stage=stage,
                variant=key,
                inputs=manifest.identity,
                provider=provider,
                request=request,
                evidence_dir=evidence_dir,
            )
            candidate = manifest.model_copy(
                update={"publications": {**manifest.publications, key: receipt}}
            )
            write_comparison_manifest(root, candidate)
            manifest.publications = candidate.publications
            distributions[key] = result
            return result

        resource = Resource(
            title=f"Publish comparison recipe {variant}",
            acquire=acquire,
            release=lambda _inputs, _value: None,
            requires=tuple(resources[-1:]),
        )
        resources.append(resource)

    class Ready(Task[None]):
        title = "Comparison recipe publications verified"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            for resource in resources:
                _ = inputs.resource(resource)
            return TaskOutcome()

    workflow = Workflow(workflow_id="comparison-prepare")
    _ = workflow.add(Ready(), requires=tuple(resources))
    _ = workflow.run()
    if set(distributions) != set(stage.runs):
        raise RuntimeError("Comparison preparation did not verify every distribution")
    return PreparedComparison(stage.remote_root / "source", distributions)

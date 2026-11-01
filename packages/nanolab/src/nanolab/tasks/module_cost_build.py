"""Remote recipe assembly using the existing Gradle/distribution contract."""

import base64
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml
from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.comparison.cells import write_json_atomically
from nanolab.comparison.module_cost import module_cost_cells
from nanolab.config.module_cost import ModuleCostConfig
from nanolab.release.recipe_execution import (
    _INVENTORY_SCRIPT,
    capture_release_inventory,
    verify_source_inventory,
)
from nanolab.tasks.recipes.workflow import read_distribution, recipe_command
from nanolab.tasks.vm.sync import REPO_SYNC_EXCLUDE_PATTERNS, REPO_SYNC_GITIGNORE_FILTER
from nanolab.workspace.provenance import git_provenance, source_fingerprint


def command(
    executor: CommandTaskExecutor,
    role: str,
    argv: tuple[str, ...],
    *,
    cwd: str | None = None,
):
    """Execute through the bound VM role; never interpolate a shell command."""
    result = executor.run(
        CommandTaskSpec(
            task_id="module-cost",
            summary=argv[0],
            role=role,
            argv=argv,
            options=CommandOptions(remote_dir=cwd),
        )
    )
    if result.return_code:
        raise RuntimeError(result.stdout + "\n" + result.stderr + f"\n{argv[0]} failed")
    return result


def stage_bytes(
    executor: CommandTaskExecutor, role: str, path: Path, content: bytes
) -> None:
    """Create once in bounded chunks, below SSH and Linux single-argument limits."""
    data = base64.b64encode(content).decode()
    for offset in range(0, max(1, len(data)), 48000):
        mode = "xb" if offset == 0 else "ab"
        command(
            executor,
            role,
            (
                "python3",
                "-c",
                "import pathlib,base64,sys;p=pathlib.Path(sys.argv[1]);"
                "p.parent.mkdir(parents=True,exist_ok=True);"
                f"f=p.open('{mode}');f.write(base64.b64decode(sys.argv[2]));f.close()",
                str(path),
                data[offset : offset + 48000],
            ),
        )


@dataclass
class BuildModuleCost(Task[dict[str, Any]]):
    """Build all images before the first measured deployment, keeping failures."""

    protocol: ModuleCostConfig
    source: Path
    run_dir: Path
    remote_root: Path
    remote_source: Path
    executor: CommandTaskExecutor
    title: str = "Freeze and assemble all module-cost recipes"

    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        """Verify synchronized sources and build all frozen recipes."""
        del inputs
        self.run_dir.mkdir(parents=True, exist_ok=True)
        evidence = self.run_dir / "inputs"
        evidence.mkdir(exist_ok=False)
        corpus = self.source / "functions/test-data/word-stats"
        for name in ("performance-medium.json", "runtime-contract.json"):
            shutil.copyfile(corpus / name, evidence / name)
        load_inputs(
            evidence / "performance-medium.json", evidence / "runtime-contract.json"
        )
        from nanolab.workspace.paths import bundled_assets_root

        for name in ("module-cost.js", "word-stats-oracle.js"):
            shutil.copyfile(bundled_assets_root() / "k6" / name, evidence / name)
        protocol = {
            "schemaVersion": 1,
            "nanolab_source": git_provenance(Path(__file__).resolve().parents[5]),
            "protocol": self.protocol.model_dump(by_alias=True),
            "source": git_provenance(self.source),
            "source_fingerprint": source_fingerprint(self.source),
            "phases": {
                "warmup_seconds": 60,
                "idle_seconds": 180,
                "idle_window_seconds": 120,
                "load_seconds": 180,
                "rate": 40,
                "drain_seconds": 40,
            },
            "cpu_memory_boundary": "control-plane container; function separate",
            "heap_mib": 64,
            "concurrency": 2,
            "replicas": 1,
            "queue": 20,
            "retries": 0,
        }
        host = command(
            self.executor,
            "stack",
            (
                "python3",
                "-c",
                "import json,platform,pathlib;print(json.dumps({"
                "'kernel':platform.release(),'machine':platform.machine(),"
                "'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id')"
                ".read_text().strip()}))",
            ),
        )
        protocol["host"] = json.loads(host.stdout)
        write_json_atomically(self.run_dir / "protocol.json", protocol)
        # Mirror exactly the normal bootstrap filters before comparing source bytes.
        with TemporaryDirectory(prefix="module-cost-source-") as temporary:
            subprocess.run(
                (
                    "rsync",
                    "-a",
                    "--filter",
                    REPO_SYNC_GITIGNORE_FILTER,
                    *(f"--exclude={pattern}" for pattern in REPO_SYNC_EXCLUDE_PATTERNS),
                    str(self.source) + "/",
                    temporary + "/",
                ),
                check=True,
                timeout=600,
            )
            capture_release_inventory(
                Path(temporary), evidence / "source-inventory.json"
            )
        expected_source = json.loads((evidence / "source-inventory.json").read_text())
        capacity = json.loads(
            command(
                self.executor,
                "stack",
                (
                    "python3",
                    "-c",
                    "import os,json;print(json.dumps({'cpus':os.cpu_count(),"
                    "'memory_mib':os.sysconf('SC_PHYS_PAGES')*"
                    "os.sysconf('SC_PAGE_SIZE')//1048576}))",
                ),
            ).stdout
        )
        protocol["build_capacity"] = capacity
        write_json_atomically(self.run_dir / "protocol.json", protocol)
        built = {}
        for arm, profile in module_cost_cells(self.protocol.block_id).items():
            destination = self.run_dir / "build" / arm
            destination.mkdir(parents=True, exist_ok=False)
            recipe = destination / "recipe.yaml"
            recipe.write_text(yaml.safe_dump(profile, sort_keys=False))
            remote = self.remote_root / arm
            stage_bytes(
                self.executor, "stack", remote / "recipe.yaml", recipe.read_bytes()
            )
            try:
                remote_inventory = remote / "source-inventory.json"
                command(
                    self.executor,
                    "stack",
                    (
                        "python3",
                        "-c",
                        _INVENTORY_SCRIPT,
                        str(self.remote_source),
                        str(remote_inventory),
                    ),
                )
                current = json.loads(
                    command(
                        self.executor, "stack", ("cat", str(remote_inventory))
                    ).stdout
                )
                write_json_atomically(destination / "source-inventory.json", current)
                verify_source_inventory(expected_source, current, self.remote_source)
                argv = (
                    *recipe_command(
                        "assembleRecipe",
                        recipe=str(remote / "recipe.yaml"),
                        output=str(remote / "output"),
                        tag=self.protocol.block_id,
                        native_build_memory=f"{capacity['memory_mib']}m",
                        native_parallelism=capacity["cpus"],
                    ),
                    "-Dorg.gradle.jvmargs=-XX:MaxRAMPercentage=100",
                    "--max-workers=" + str(capacity["cpus"]),
                )
                write_json_atomically(
                    destination / "command.json",
                    {"argv": argv, "cwd": str(self.remote_source)},
                )
                result = command(
                    self.executor, "stack", argv, cwd=str(self.remote_source)
                )
                (destination / "build.log").write_text(
                    result.stdout + "\n" + result.stderr
                )
                report = destination / "distribution.json"
                report.write_text(
                    command(
                        self.executor,
                        "stack",
                        ("cat", str(remote / "output/distribution.json")),
                    ).stdout
                )
                distribution = read_distribution(
                    report, recipe=recipe, tag=self.protocol.block_id, published=False
                )
                images = {}
                for component in distribution.components:
                    ref = component.image.reference
                    command(self.executor, "stack", ("docker", "push", ref))
                    image = json.loads(
                        command(
                            self.executor, "stack", ("docker", "image", "inspect", ref)
                        ).stdout
                    )[0]
                    if image["Id"] != component.image.id:
                        raise ValueError(
                            "Built image identity changed before deployment"
                        )
                    digests = [
                        d
                        for d in image["RepoDigests"]
                        if d.startswith(ref.rsplit(":", 1)[0] + "@")
                    ]
                    if len(digests) != 1:
                        raise ValueError("Missing unique local registry digest")
                    images[component.kind] = {
                        "reference": ref,
                        "digest": digests[0],
                        "config_id": image["Id"],
                        "uncompressed_image_bytes": image["Size"],
                        "config": image["Config"],
                    }
                write_json_atomically(destination / "images.json", images)
                # Read artifact sizes without starting the measured process.
                cp_ref = distribution.control_plane().image.reference
                container = command(
                    self.executor, "stack", ("docker", "create", cp_ref)
                ).stdout.strip()
                try:
                    # Both recipe image modes install the application under /app.
                    command(
                        self.executor,
                        "stack",
                        ("docker", "cp", f"{container}:/app", str(remote / "artifact")),
                    )
                    artifact = command(
                        self.executor,
                        "stack",
                        (
                            "python3",
                            "-c",
                            "import pathlib,json,sys;root=pathlib.Path(sys.argv[1]);"
                            "print(json.dumps({str(p.relative_to(root)):"
                            "p.stat().st_size "
                            "for p in root.rglob('*') if p.is_file()}))",
                            str(remote / "artifact"),
                        ),
                    )
                    write_json_atomically(
                        destination / "artifact-files.json", json.loads(artifact.stdout)
                    )
                finally:
                    command(self.executor, "stack", ("docker", "rm", container))
                built[arm] = distribution
            except BaseException as error:
                (destination / "failure.txt").write_text(str(error))
                raise
        return TaskOutcome(value=built)

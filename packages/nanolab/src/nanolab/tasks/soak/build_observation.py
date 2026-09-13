"""Produce request-bound evidence from owned commands and instrumented build stages.

Instrumentation is generated only in a materialized workspace. Its bytes and the
effective recipe are identified separately from the original, dirty-inclusive
source snapshot. Version records must originate in a successful command's log;
Dockerfile text, requested versions and the observer's PATH are never evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanolab.tasks.soak.artifacts import fingerprint
from nanolab.tasks.soak.images import BuildRecipe

_GRADLE_OPTIONS = (
    "--no-configuration-cache",
    "--no-build-cache",
    "--rerun-tasks",
    "--no-parallel",
    "--console=plain",
    "--info",
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

# ProcessBuilder starts version commands as children of the owned Gradle process.
# Bounded reading and a deadline apply even if a selected executable malfunctions.
_GRADLE_INIT = r"""
import groovy.json.JsonOutput
import org.gradle.api.tasks.compile.JavaCompile
import org.gradle.api.logging.StandardOutputListener
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.security.MessageDigest

def marker = __MARKER__
def stage = __STAGE__
def hashFile = { File file ->
    def digest = MessageDigest.getInstance('SHA-256')
    file.withInputStream { stream ->
        byte[] buffer = new byte[65536]
        int count
        while ((count = stream.read(buffer)) != -1) digest.update(buffer, 0, count)
    }
    digest.digest().encodeHex().toString()
}
def version = { String name, File executable, File cwd, Map details, Map env ->
    def argv = [executable.absolutePath, '--version']
    def builder = new ProcessBuilder(argv).directory(cwd).redirectErrorStream(true)
    builder.environment().putAll(env)
    def process = builder.start()
    def output = new ByteArrayOutputStream()
    def failure = new AtomicReference()
    def reader = new Thread({
        try {
            byte[] buffer = new byte[4096]
            int count
            while ((count = process.inputStream.read(buffer)) != -1) {
                if (output.size() + count > 65536) {
                    process.destroyForcibly()
                    throw new GradleException('Observation version output exceeds limit')
                }
                output.write(buffer, 0, count)
            }
        } catch (Throwable error) { failure.set(error) }
    } as Runnable)
    reader.daemon = true
    reader.start()
    if (!process.waitFor(30, TimeUnit.SECONDS)) {
        process.destroyForcibly()
        throw new GradleException('Observation version command timed out')
    }
    reader.join(1000)
    if (reader.alive || failure.get() != null || process.exitValue() != 0)
        throw new GradleException('Observation version command failed', failure.get())
    def record = details + [toolchain: name, argv: argv,
        execution_cwd: cwd.absolutePath, exit_code: process.exitValue(),
        output: output.toString('UTF-8'), stage: stage,
        executable_sha256: hashFile(executable)]
    println('\n' + marker + JsonOutput.toJson(record).getBytes('UTF-8').encodeBase64())
}
gradle.settingsEvaluated { settings ->
    def executable = new File(gradle.gradleHomeDir, 'bin/gradle')
    version('gradle', executable, settings.settingsDir,
        [gradle_home: gradle.gradleHomeDir.absolutePath,
         effective_gradle_version: gradle.gradleVersion],
        [JAVA_HOME: System.getProperty('java.home')])
}

// Record the selected compiler only when its JavaCompile action actually runs.
// Reject an explicit fork override which would bypass that selected toolchain.
gradle.allprojects { project ->
    project.tasks.withType(JavaCompile).configureEach { task ->
        def selected = [:]
        task.doFirst {
            def compiler = task.javaCompiler.get()
            def executable = compiler.executablePath.asFile.canonicalFile
            def override = task.options.forkOptions.executable
            if (task.options.fork && override != null &&
                    new File(override).canonicalFile != executable)
                throw new GradleException('Unobservable JavaCompile fork override')
            selected.compiler = executable
            selected.home = compiler.metadata.installationPath.asFile
            selected.hash = hashFile(executable)
        }
        task.doLast {
            def executable = task.javaCompiler.get().executablePath.asFile.canonicalFile
            if (executable != selected.compiler || hashFile(executable) != selected.hash)
                throw new GradleException('JavaCompile compiler changed during execution')
            version('java', new File(selected.home, 'bin/java'), project.rootDir,
                [task: task.path, compiler: executable.absolutePath,
                 compiler_sha256: selected.hash], [:])
        }
    }
}

// Gradle --info logs the command actually handed to ExecOperations. Observe
// that executable, not GRAALVM_HOME or an independently resolved native-image.
// doLast proves the native task completed; a missing process event fails closed.
if (stage != null) {
    def active = new AtomicReference()
    def launched = new java.util.concurrent.ConcurrentHashMap()
    def listener = { CharSequence message ->
        def task = active.get()
        if (task != null) {
            message.toString().readLines().each { line ->
                def match = line =~ /Starting process 'command '([^']*\/native-image)''.*Command: (.*)/
                if (match.find() && !(match.group(2) =~ /(?:^|\s)--version(?:\s|$)/).find())
                    launched[task] = [executable: match.group(1), command: match.group(2)]
            }
        }
    } as StandardOutputListener
    gradle.rootProject { project -> project.logging.addStandardOutputListener(listener) }
    gradle.allprojects { project ->
        project.tasks.configureEach { task ->
            if (task.class.name.startsWith('org.graalvm.buildtools.gradle.tasks.BuildNativeImageTask')) {
                task.doFirst { active.set(task.path); launched.remove(task.path) }
                task.doLast {
                    def command = launched.remove(task.path)
                    active.set(null)
                    if (command == null)
                        throw new GradleException('Actual native-image process was not observed')
                    def env = task.options.get().environmentVariables.get()
                    version('native-image', new File(command.executable), project.rootDir,
                        [task: task.path, compiler_command: command.command], env)
                }
            }
        }
    }
}
"""

_NODE_PRELOAD = r"""
const {spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const env = {...process.env};
delete env.NODE_OPTIONS;
const result = spawnSync(process.execPath, ['--version'], {
  cwd: process.cwd(), env, encoding: 'utf8', timeout: 30000, maxBuffer: 65536,
  stdio: ['ignore', 'pipe', 'pipe']
});
if (result.error || result.status !== 0) throw new Error('Node version observation failed: ' +
  JSON.stringify({error: result.error && result.error.message, status: result.status,
                  signal: result.signal, stderr: String(result.stderr).slice(0, 1024)}));
const record = {
  toolchain: 'node', argv: [process.execPath, '--version'],
  execution_cwd: process.cwd(), exit_code: result.status,
  output: result.stdout + result.stderr, stage: __STAGE__,
  process_argv: process.argv,
  executable_sha256: createHash('sha256').update(readFileSync(process.execPath)).digest('hex')
};
process.stderr.write('\n' + __MARKER__ + Buffer.from(JSON.stringify(record)).toString('base64') + '\n');
"""


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")


def _canonical_image(reference: str) -> str:
    name, digest = reference.rsplit("@", 1)
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    name = name.removeprefix("docker.io/").removeprefix("index.docker.io/")
    name = name.removeprefix("library/")
    return name + "@" + digest


class BuildObservationCapture:
    """One role's observation lifecycle; this class never starts a build itself."""

    def __init__(
        self,
        recipe: BuildRecipe,
        metadata: Path,
        workspace: Path,
        *,
        source_fingerprint: str,
        artifact_limit_bytes: int,
    ) -> None:
        """Bind an isolated build workspace and finite evidence budget."""
        if type(artifact_limit_bytes) is not int or artifact_limit_bytes <= 0:
            raise ValueError("positive observation artifact budget is required")
        if not source_fingerprint:
            raise ValueError("source fingerprint is required")
        self.original = recipe
        self.recipe = recipe
        self.metadata = metadata.absolute()
        self.workspace = workspace.resolve(strict=True)
        self.source_fingerprint = source_fingerprint
        self.limit = artifact_limit_bytes
        self.spent = 0
        self.token = uuid4().hex
        self.marker = "NANOLAB_BUILD_OBSERVATION_" + self.token + ":"
        self.directory = self.metadata.parent / ("observation-" + self.token)
        self.instrumentation_path = self.directory / "instrumentation.json"
        self.commands: list[dict[str, Any]] = []
        self.evidence_paths: list[Path] = []
        self.request: dict[str, Any] | None = None
        self.build_argv: tuple[str, ...] | None = None
        self.image_digest: str | None = None
        self._generated: list[dict[str, Any]] = []
        self._toolchains: dict[str, tuple[tuple[str, ...], str, str | None]] = {}

    def _write(self, path: Path, body: bytes) -> dict[str, Any]:
        if len(body) > self.limit - self.spent:
            raise ValueError("observation artifact byte budget exhausted")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(body)
        self.spent += len(body)
        self.evidence_paths.append(path)
        return {
            "path": str(path.relative_to(self.metadata.parent)),
            "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
        }

    def _read(self, path: Path) -> bytes:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("observation log must be a regular non-symlink file")
        with path.open("rb") as stream:
            body = stream.read(min(self.limit, 16 * 1024 * 1024) + 1)
        if len(body) > min(self.limit, 16 * 1024 * 1024):
            raise ValueError("observation input exceeds byte limit")
        return body

    def _generate(self, path: Path, text: str) -> None:
        body = text.encode("utf-8")
        descriptor = self._write(path, body)
        self._generated.append(descriptor)

    def prepare(self) -> BuildRecipe:
        """Generate explicit instrumentation and return the effective build recipe."""
        bake = deepcopy(self.original.bake)
        if not bake or len(bake.get("target", {})) != 1:
            raise ValueError("instrumentation requires one build target")
        target = next(iter(bake["target"].values()))
        raw_context = Path(target.get("context", "."))
        context = (self.workspace / raw_context).resolve(strict=True)
        if not context.is_dir() or not context.is_relative_to(self.workspace):
            raise ValueError("build context escapes the bound workspace")
        if target.get("contexts") or target.get("dockerfile-inline"):
            raise ValueError(
                "custom contexts/inline recipes require explicit instrumentation"
            )
        target["context"] = str(context)
        generated = context / (".nanolab-observation-" + self.token)
        prerequisite = self.original.prerequisite_argv
        if prerequisite is not None:
            if Path(prerequisite[0]).name not in {"gradle", "gradlew"}:
                raise ValueError(
                    "prerequisite requires Gradle compiler instrumentation"
                )
            init = generated / "observe.gradle"
            self._generate(
                init,
                _GRADLE_INIT.replace("__MARKER__", json.dumps(self.marker)).replace(
                    "__STAGE__", "null"
                ),
            )
            prerequisite = (*prerequisite, "--init-script", str(init), *_GRADLE_OPTIONS)
        else:
            dockerfile = (context / target.get("dockerfile", "Dockerfile")).resolve(
                strict=True
            )
            if not dockerfile.is_relative_to(self.workspace):
                raise ValueError("Dockerfile escapes the bound workspace")
            text = self._read(dockerfile).decode("utf-8")
            native = self.original.variant.startswith("native-")
            if not native and self.original.variant != "default":
                raise ValueError("unsupported build-stage instrumentation")
            lines, current, stages, injected = [], None, set(), 0
            # Instrument only the catalogue's shell-form npm/Gradle build steps.
            # Each stage gets a distinct hook carrying its stage identity.
            for line in text.splitlines():
                match = re.match(r"(?i)^FROM\s+\S+\s+AS\s+([\w.-]+)\s*$", line)
                if re.match(r"(?i)^FROM\s", line):
                    current = match.group(1) if match else None
                if re.match(r"(?i)^\s*(SHELL|ONBUILD)\s", line):
                    raise ValueError(
                        "custom shell/onbuild requires explicit instrumentation"
                    )
                command = re.match(r"^RUN\s+(\./gradlew|npm)\s+", line)
                relevant = command and command.group(1) == (
                    "./gradlew" if native else "npm"
                )
                if relevant:
                    if current is None:
                        raise ValueError("instrumented build stage must have a name")
                    extension = "gradle" if native else "cjs"
                    name = f"observe-{current}.{extension}"
                    destination = "/nanolab-observation-" + self.token + "/" + name
                    if current not in stages:
                        hook = _GRADLE_INIT if native else _NODE_PRELOAD
                        self._generate(
                            generated / name,
                            hook.replace("__MARKER__", json.dumps(self.marker)).replace(
                                "__STAGE__", json.dumps(current)
                            ),
                        )
                        lines.append(f"COPY {generated.name}/{name} {destination}")
                        stages.add(current)
                    if native:
                        options = " ".join(_GRADLE_OPTIONS)
                        line = line.replace(
                            "./gradlew",
                            f"./gradlew --init-script {destination} {options}",
                            1,
                        )
                    else:
                        line = line.replace(
                            "RUN ",
                            f'RUN NODE_OPTIONS="--require={destination} '
                            f'${{NODE_OPTIONS:-}}" ',
                            1,
                        )
                    injected += 1
                lines.append(line)
            if not injected:
                raise ValueError(
                    "recipe has no supported build-stage instrumentation point"
                )
            generated_dockerfile = "\n".join(lines) + "\n"
            self._generate(generated / "Dockerfile", generated_dockerfile)
            target.pop("dockerfile", None)
            # Bake runs HCL interpolation over this value, and a Dockerfile is
            # not HCL: "${NODE_OPTIONS:-}" is a parse error and a plain
            # "${VAR}" would be substituted away silently. "$${" is bake's
            # escape and reaches the Dockerfile parser as "${".
            target["dockerfile-inline"] = generated_dockerfile.replace("${", "$${")
        identity = fingerprint(
            {
                "original_recipe_fingerprint": self.original.recipe_fingerprint,
                "bake": bake,
                "prerequisite_argv": prerequisite,
                "generated_files": self._generated,
            }
        )
        self.recipe = replace(
            self.original,
            bake=bake,
            prerequisite_argv=prerequisite,
            recipe_fingerprint=identity,
        )
        self._write(
            self.instrumentation_path,
            _json_bytes(
                {
                    "schema": "nanolab-soak-build-instrumentation-v1",
                    "source_fingerprint": self.source_fingerprint,
                    "workspace": str(self.workspace),
                    "original_recipe_fingerprint": self.original.recipe_fingerprint,
                    "original_bake": self.original.bake,
                    "original_prerequisite_argv": self.original.prerequisite_argv,
                    "effective_recipe_fingerprint": identity,
                    "effective_bake": bake,
                    "effective_prerequisite_argv": prerequisite,
                    "generated_files": self._generated,
                }
            ),
        )
        return self.recipe

    def start(self, build_argv: tuple[str, ...]) -> None:
        """Exclusively persist the request before any command is executed."""
        from nanolab.tasks.soak.build_provenance import write_observation_request

        if self.request is not None:
            raise ValueError("observation request already started")
        path = write_observation_request(
            self.recipe, self.metadata, self.workspace, build_argv=build_argv
        )
        self.request = json.loads(self._read(path))
        self.build_argv = build_argv

    def record(
        self, kind: str, argv: tuple[str, ...], executor: Any, **extra: Any
    ) -> None:
        """Capture the immediately preceding owned command, retaining failed results."""
        if self.request is None:
            raise ValueError("observation request must precede commands")
        log, result = executor.last_log_path, executor.last_result
        if log is None or result is None:
            raise ValueError("owned command result/log unavailable")
        body = self._read(Path(log))
        descriptor = self._write(
            self.directory / f"command-{len(self.commands)}.log", body
        )
        fields = (
            "returncode",
            "reaped",
            "cancelled",
            "timed_out",
            "quota_exceeded",
            "forced_stop",
            "errors",
            "log_bytes",
        )
        state = {name: getattr(result, name, None) for name in fields}
        item = {
            "kind": kind,
            "argv": list(argv),
            "cwd": str(self.workspace),
            "exit_code": result.returncode,
            "log": descriptor,
            "result": state,
            **extra,
        }
        self.commands.append(item)
        if (
            type(result.returncode) is not int
            or result.returncode != 0
            or result.reaped is not True
            or result.log_bytes != len(body)
            or any(
                state[name]
                for name in (
                    "cancelled",
                    "timed_out",
                    "quota_exceeded",
                    "forced_stop",
                    "errors",
                )
            )
        ):
            raise ValueError("owned command did not complete successfully")
        if (
            kind in {"prerequisite", "build"}
            and list(argv) != self.request[kind + "_argv"]
        ):
            raise ValueError("owned command differs from requested argv")

    def capture_toolchains(
        self, kind: str, *, materials: Mapping[str, str] | None = None
    ) -> None:
        """Extract bounded frames from an executed command and bind stage materials."""
        parents = [item for item in self.commands if item["kind"] == kind]
        if len(parents) != 1:
            raise ValueError("one completed build/prerequisite command is required")
        parent = parents[0]
        body = self._read(self.metadata.parent / parent["log"]["path"])
        text = body.decode("utf-8", "strict")
        pattern = re.compile(
            r"^(?:#\d+\s+\d+(?:\.\d+)?\s+)?"
            + re.escape(self.marker)
            + r"([A-Za-z0-9+/=]+)\s*$",
            re.MULTILINE,
        )
        matches = list(pattern.finditer(text))
        if not matches or len(matches) > 4096:
            raise ValueError(
                "missing or excessive actual build-stage toolchain observations"
            )
        for match in matches:
            if len(match.group(1)) > 128 * 1024:
                raise ValueError("toolchain frame exceeds byte limit")
            record = json.loads(base64.b64decode(match.group(1), validate=True))
            name, argv, output = (
                record.get("toolchain"),
                record.get("argv"),
                record.get("output"),
            )
            allowed = {
                "java": {"java"},
                "gradle": {"gradle", "gradlew"},
                "node": {"node"},
                "native-image": {"native-image"},
            }
            if (
                name not in allowed
                or not isinstance(argv, list)
                or len(argv) != 2
                or not all(isinstance(arg, str) and arg for arg in argv)
                or Path(argv[0]).name not in allowed[name]
                or argv[1] not in {"--version", "-version"}
                or type(record.get("exit_code")) is not int
                or record["exit_code"] != 0
                or not isinstance(output, str)
                or not output
                or not record.get("execution_cwd")
            ):
                raise ValueError("invalid actual toolchain command frame")
            if name == "java" and not record.get("compiler"):
                raise ValueError("Java observation lacks actual JavaCompile compiler")
            if name == "native-image" and not record.get("compiler_command"):
                raise ValueError("native observation lacks actual compiler invocation")
            if (
                name == "gradle"
                and record.get("effective_gradle_version") is not None
                and re.findall(r"(?m)^Gradle (\S+)\s*$", output)
                != [record["effective_gradle_version"]]
            ):
                raise ValueError("observed Gradle differs from executing distribution")
            material = None
            if kind == "build":
                stage = record.get("stage")
                if not isinstance(stage, str) or not stage:
                    raise ValueError("build-stage observation lacks stage")
                references = re.findall(
                    r"(?m)^#\d+ \["
                    + re.escape(stage)
                    + r"(?: [^\]]*)?\] FROM (\S+@sha256:[0-9a-f]{64})(?:\s|$)",
                    text,
                )
                candidates = {
                    value
                    for value in (materials or {}).values()
                    if any(
                        _canonical_image(ref) == _canonical_image(value)
                        for ref in references
                    )
                }
                if len(candidates) != 1:
                    raise ValueError(
                        "build-stage toolchain material is missing or ambiguous"
                    )
                material = candidates.pop()
            identity = (tuple(argv), output, material)
            if name in self._toolchains:
                if self._toolchains[name] != identity:
                    raise ValueError(
                        "conflicting actual compiler/toolchain observations"
                    )
                continue
            self._toolchains[name] = identity
            log = self._write(
                self.directory / f"version-{name}.log", output.encode("utf-8")
            )
            item = {key: value for key, value in record.items() if key != "output"}
            item.update(
                kind="toolchain",
                cwd=str(self.workspace),
                scope="host" if kind == "prerequisite" else "build",
                log=log,
                parent_log=parent["log"],
                frame_sha256=hashlib.sha256(match.group(0).encode()).hexdigest(),
            )
            if material is not None:
                item["material"] = material
            self.commands.append(item)

    def require_toolchains(self, names: set[str]) -> None:
        """Reject skipped compiler tasks or incomplete instrumentation."""
        missing = names - self._toolchains.keys()
        if missing:
            raise ValueError("missing actual toolchains: " + ", ".join(sorted(missing)))

    def complete(
        self, execute: Callable[..., None], observe: Callable[..., bytes]
    ) -> None:
        """Observe the actual builder/materials and persist the completed sidecar."""
        from nanolab.tasks.soak.build_provenance import _materials, _predicate

        record = json.loads(self._read(self.metadata))
        if "containerimage.digest" not in record:
            assert self.recipe.bake is not None
            target = next(iter(self.recipe.bake["target"]))
            record = record[target]
        digest, build_ref = (
            record.get("containerimage.digest"),
            record.get("buildx.build.ref"),
        )
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("observed build digest unavailable")
        if not isinstance(build_ref, str) or len(build_ref.split("/")) != 3:
            raise ValueError("observed builder reference unavailable")
        builder, node, invocation = build_ref.split("/")
        if not all(
            re.fullmatch(r"[\w.-]+", part) for part in (builder, node, invocation)
        ):
            raise ValueError("invalid observed builder reference")
        self.image_digest = digest
        execute(
            "toolchain",
            ("docker", "buildx", "inspect", builder),
            toolchain="buildkit",
            scope="builder",
            build_ref=build_ref,
        )
        # Keep full inspect output, but give the collector the actual node's
        # section so unrelated nodes cannot create an ambiguous version.
        item = self.commands[-1]
        raw = self._read(self.metadata.parent / item["log"]["path"]).decode("utf-8")
        sections = re.split(r"(?m)(?=^\s*Name:\s*\S+\s*$)", raw)
        selected = [
            section
            for section in sections
            if re.match(r"\s*Name:\s*" + re.escape(node) + r"\s*\n", section)
            and re.search(r"(?m)^\s*BuildKit(?: version)?:", section)
        ]
        if len(selected) != 1:
            raise ValueError("actual builder node/version is missing or ambiguous")
        item["raw_log"] = item["log"]
        item["log"] = self._write(
            self.directory / "builder-node.log", selected[0].encode()
        )
        if self.recipe.prerequisite_argv is None:
            local = record.get("buildx.build.provenance")
            if local:
                materials = _materials(_predicate(local, self.recipe.platform, digest))
            else:
                image = self.recipe.image
                repository = (
                    image.rsplit(":", 1)[0]
                    if ":" in image.rsplit("/", 1)[-1]
                    else image
                )
                body = observe(
                    (
                        "docker",
                        "buildx",
                        "imagetools",
                        "inspect",
                        repository + "@" + digest,
                        "--format",
                        "{{json .Provenance}}",
                    ),
                    30,
                )
                self._write(self.directory / "stage-materials.json", body)
                materials = _materials(
                    _predicate(json.loads(body), self.recipe.platform, digest)
                )
            self.capture_toolchains("build", materials=materials)
            self.require_toolchains(
                {
                    "native-image"
                    if self.recipe.variant.startswith("native-")
                    else "node"
                }
            )
        else:
            self.require_toolchains({"java", "gradle"})
        self._finish("complete")

    def _finish(self, status: str, reason: str | None = None) -> None:
        if self.request is None:
            return
        self._write(
            self.metadata.with_name(self.metadata.name + ".observations.json"),
            _json_bytes(
                {
                    "schema": "nanolab-soak-build-observations-v1",
                    "request_id": self.request["request_id"],
                    "recipe_fingerprint": self.recipe.recipe_fingerprint,
                    "workspace": str(self.workspace),
                    "image_digest": self.image_digest,
                    "source_fingerprint": self.source_fingerprint,
                    "instrumentation": str(
                        self.instrumentation_path.relative_to(self.metadata.parent)
                    ),
                    "commands": self.commands,
                    "status": status,
                    "reason": reason,
                }
            ),
        )

    def fail(self, error: BaseException) -> None:
        """Preserve partial command evidence without certifying a failed build."""
        path = self.metadata.with_name(self.metadata.name + ".observations.json")
        if not path.exists():
            self._finish("incomplete", str(error)[:1024])

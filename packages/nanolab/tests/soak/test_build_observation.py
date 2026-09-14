"""Synthetic transport tests; never execute Docker, Gradle, or source-control tools."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanolab.tasks.soak.images import BuildRecipe


def recipe_for(work, variant="jvm"):
    dockerfile = work / "Dockerfile"
    dockerfile.write_text(
        "FROM node:20-alpine AS build\nWORKDIR /src\nRUN npm run build\n"
        "FROM node:20-alpine\nCOPY --from=build /src/dist /app\n"
        if variant == "default"
        else "FROM oraclelinux:9-slim AS builder\nWORKDIR /workspace\n"
        'RUN ./gradlew "$NATIVE_TASK" $GRADLE_ARGS --no-daemon\n'
        "FROM scratch\nCOPY --from=builder /tmp/application /app\n"
    )
    image = "localhost:5000/example:run"
    return BuildRecipe(
        "example",
        "build",
        variant,
        "linux/amd64",
        image,
        ("./gradlew", "bootJar", "--no-daemon") if variant == "jvm" else None,
        {
            "target": {
                "example": {
                    "context": ".",
                    "dockerfile": "Dockerfile",
                    "tags": [image],
                    "platforms": ["linux/amd64"],
                }
            }
        },
        "original-recipe",
        None,
    )


def prepare(tmp_path, variant="jvm", limit=1024 * 1024):
    from nanolab.tasks.soak.build_observation import BuildObservationCapture

    work = tmp_path / "workspace"
    work.mkdir()
    original = recipe_for(work, variant)
    capture = BuildObservationCapture(
        original,
        tmp_path / "metadata.json",
        work,
        source_fingerprint="dirty-source",
        artifact_limit_bytes=limit,
    )
    effective = capture.prepare()
    bake = tmp_path / "bake.json"
    bake.write_text(json.dumps(effective.bake))
    argv = (
        "docker",
        "buildx",
        "bake",
        "-f",
        str(bake),
        "--push",
        "--provenance=mode=max",
        "--metadata-file",
        str(capture.metadata),
        "--progress=plain",
    )
    capture.start(argv)
    return capture, original, effective, argv


def transport(tmp_path, text, *, code=0, **changes):
    log = tmp_path / "transport.log"
    log.write_text(text)
    result = {
        "returncode": code,
        "reaped": True,
        "cancelled": False,
        "timed_out": False,
        "quota_exceeded": False,
        "forced_stop": False,
        "errors": (),
        "log_bytes": len(text.encode()),
    }
    result.update(changes)
    return SimpleNamespace(last_log_path=log, last_result=SimpleNamespace(**result))


def frame(capture, name, output, **extra):
    record = dict(
        toolchain=name,
        argv=["/selected/bin/" + name, "--version"],
        output=output,
        exit_code=0,
        execution_cwd="/stage",
        task=":compileJava",
        compiler="/selected/bin/javac",
        **extra,
    )
    return capture.marker + base64.b64encode(json.dumps(record).encode()).decode()


def test_preparation_binds_generated_recipe_without_changing_original(tmp_path):
    capture, original, effective, argv = prepare(tmp_path)
    assert effective.prerequisite_argv is not None
    assert original.prerequisite_argv == ("./gradlew", "bootJar", "--no-daemon")
    assert "--init-script" in effective.prerequisite_argv
    assert effective.recipe_fingerprint != original.recipe_fingerprint
    request = json.loads(Path(str(capture.metadata) + ".request.json").read_text())
    assert request["prerequisite_argv"] == list(effective.prerequisite_argv)
    assert request["build_argv"] == list(argv)
    provenance = json.loads(capture.instrumentation_path.read_text())
    assert provenance["source_fingerprint"] == "dirty-source"
    assert provenance["original_recipe_fingerprint"] == "original-recipe"
    assert provenance["effective_recipe_fingerprint"] == effective.recipe_fingerprint
    assert provenance["generated_files"]


def test_records_real_transport_result_and_copies_logs_into_evidence_root(tmp_path):
    capture, _, effective, _ = prepare(tmp_path)
    executor = transport(tmp_path, "BUILD SUCCESSFUL\n")
    capture.record("prerequisite", effective.prerequisite_argv, executor)  # pyright: ignore[reportArgumentType]
    item = capture.commands[0]
    assert item["exit_code"] == 0
    assert item["argv"] == list(effective.prerequisite_argv)  # pyright: ignore[reportArgumentType]
    path = capture.metadata.parent / item["log"]["path"]
    assert path.read_bytes() == b"BUILD SUCCESSFUL\n"
    assert item["log"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"reaped": False},
        {"timed_out": True},
        {"quota_exceeded": True},
        {"cancelled": True},
        {"forced_stop": True},
        {"returncode": 1},
    ],
)
def test_incomplete_transport_cannot_be_recorded_as_success(tmp_path, changes):
    capture, _, effective, _ = prepare(tmp_path)
    executor = transport(tmp_path, "partial\n", **changes)
    with pytest.raises(ValueError, match=r"owned command did not complete"):
        capture.record("prerequisite", effective.prerequisite_argv, executor)  # pyright: ignore[reportArgumentType]
    assert capture.commands[0]["result"][next(iter(changes))] == next(
        iter(changes.values())
    )


def test_extracts_compiler_evidence_from_prerequisite_log_not_path_java(tmp_path):
    capture, _, effective, _ = prepare(tmp_path)
    assert effective.prerequisite_argv is not None
    text = frame(capture, "java", 'openjdk version "25.0.1"\n') + "\n"
    capture.record(
        "prerequisite",
        effective.prerequisite_argv,
        transport(tmp_path, text),
    )
    capture.capture_toolchains("prerequisite")
    item = next(item for item in capture.commands if item.get("toolchain") == "java")
    assert item["argv"] == ["/selected/bin/java", "--version"]
    assert item["compiler"] == "/selected/bin/javac"
    assert item["scope"] == "host"
    assert item["execution_cwd"] == "/stage"


def test_conflicting_compilers_are_rejected_instead_of_selecting_one(tmp_path):
    capture, _, effective, _ = prepare(tmp_path)
    assert effective.prerequisite_argv is not None
    text = "\n".join(
        frame(capture, "java", value)
        for value in ['openjdk version "25.0.1"\n', 'openjdk version "26"\n']
    )
    capture.record(
        "prerequisite",
        effective.prerequisite_argv,
        transport(tmp_path, text),
    )
    with pytest.raises(ValueError, match="conflicting actual compiler"):
        capture.capture_toolchains("prerequisite")


@pytest.mark.parametrize(
    ("variant", "name", "stage", "version"),
    [
        ("default", "node", "build", "v20.19.0\n"),
        ("native-o3", "native-image", "builder", "native-image 25.0.1\n"),
    ],
)
def test_build_stage_evidence_requires_executed_frame_and_observed_material(
    tmp_path,
    variant,
    name,
    stage,
    version,
):
    capture, _, _, argv = prepare(tmp_path, variant)
    material = "library/node@sha256:" + "b" * 64
    text = (
        f"#5 [{stage} 1/4] FROM docker.io/library/node:20@sha256:"
        + "b" * 64
        + "\n#7 0.123 "
        + frame(
            capture,
            name,
            version,
            stage=stage,
            compiler_command="native-image -jar app.jar",
        )
        + "\n"
    )
    capture.record("build", argv, transport(tmp_path, text))
    capture.capture_toolchains("build", materials={"pkg:docker/node": material})
    item = next(item for item in capture.commands if item.get("toolchain") == name)
    assert item["material"] == material
    assert item["scope"] == "build"
    assert item["stage"] == stage


def test_dockerfile_intent_alone_never_produces_toolchain_evidence(tmp_path):
    capture, _, _, argv = prepare(tmp_path, "default")
    capture.record("build", argv, transport(tmp_path, "Build succeeded\n"))
    with pytest.raises(ValueError, match=r"missing or excessive actual"):
        capture.capture_toolchains("build", materials={})


def test_rejects_context_outside_bound_workspace_before_execution(tmp_path):
    from nanolab.tasks.soak.build_observation import BuildObservationCapture

    work = tmp_path / "work"
    work.mkdir()
    recipe = recipe_for(work)
    assert recipe.bake is not None
    recipe.bake["target"]["example"]["context"] = str(tmp_path)
    capture = BuildObservationCapture(
        recipe,
        tmp_path / "metadata.json",
        work,
        source_fingerprint="source",
        artifact_limit_bytes=100000,
    )
    with pytest.raises(ValueError, match=r"build context escapes the bound"):
        capture.prepare()


def test_rejects_unsupported_native_recipe_before_any_build(tmp_path):
    from nanolab.tasks.soak.build_observation import BuildObservationCapture

    work = tmp_path / "work"
    work.mkdir()
    recipe = recipe_for(work, "native-o3")
    (work / "Dockerfile").write_text("FROM builder\nRUN custom-native-build\n")
    capture = BuildObservationCapture(
        recipe,
        tmp_path / "metadata.json",
        work,
        source_fingerprint="source",
        artifact_limit_bytes=100000,
    )
    with pytest.raises(ValueError, match=r"recipe has no supported build-stage"):
        capture.prepare()


def test_node_preload_observes_running_executable_even_with_decoy_path_node(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node interpreter unavailable for synthetic preload execution")
    capture, _, _, _ = prepare(tmp_path, "default")
    hook = next(capture.workspace.glob(".nanolab-observation-*/observe-build.cjs"))
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "node").write_text("#!/bin/sh\nprintf 'v0.0.0\\n'\n")
    (decoy / "node").chmod(0o755)
    result = subprocess.run(
        (
            node,
            "-e",
            "console.log(JSON.stringify({exe:process.execPath,version:process.version}))",
        ),
        cwd=capture.workspace,
        env={
            **os.environ,
            "PATH": str(decoy),
            "NODE_OPTIONS": "--require=" + str(hook),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    encoded = next(
        line.removeprefix(capture.marker)
        for line in result.stderr.splitlines()
        if line.startswith(capture.marker)
    )
    observed = json.loads(base64.b64decode(encoded))
    assert observed["argv"] == [actual["exe"], "--version"]
    assert observed["output"].strip() == actual["version"]
    assert observed["output"].strip() != "v0.0.0"
    assert observed["stage"] == "build"


@pytest.mark.parametrize("variant", ["jvm", "native-o3"])
def test_generated_gradle_hook_runs_against_synthetic_task_and_selected_compiler(
    tmp_path, variant
):
    """Run Groovy directly with task doubles, never invoke a real Gradle/build."""
    java = shutil.which("java")
    libraries = sorted(
        Path.home().glob(".gradle/wrapper/dists/gradle-*/**/lib/groovy-[0-9]*.jar")
    )
    if java is None or not libraries:
        pytest.skip("local Groovy interpreter unavailable for synthetic hook execution")
    capture, _, _, _ = prepare(tmp_path, variant)
    hook = next(capture.workspace.glob(".nanolab-observation-*/observe*.gradle"))
    selected = tmp_path / "selected" / "bin"
    distribution = tmp_path / "distribution" / "bin"
    selected.mkdir(parents=True)
    distribution.mkdir(parents=True)
    for path, output in (
        (selected / "java", 'openjdk version "25.0.1"'),
        (selected / "javac", "javac 25.0.1"),
        (selected / "native-image", "native-image 25.0.1"),
        (distribution / "gradle", "Gradle 9.7.1"),
    ):
        path.write_text("#!/bin/sh\nprintf '%s\\n' '" + output + "'\n")
        path.chmod(0o755)
    harness = tmp_path / "harness.groovy"
    harness.write_text(r'''
def work = new File(args[1])
def compiler = new File(args[2])
def home = new File(args[3])
def first = [], last = []
def javaTask = new Expando(path: ':compileJava')
javaTask.javaCompiler = new Expando(get: { -> [executablePath: [asFile: new File(compiler, 'javac')],
    metadata: [installationPath: [asFile: compiler.parentFile]]] })
javaTask.options = [fork: false, forkOptions: [executable: null]]
javaTask.doFirst = { c -> first.add(c) }
javaTask.doLast = { c -> last.add(c) }
def loader = new GroovyClassLoader(this.class.classLoader)
def nativeClass = loader.parseClass("""
package org.graalvm.buildtools.gradle.tasks
class BuildNativeImageTaskSynthetic {
    String path = ':nativeCompile'
    def options = new Expando(get: { -> [environmentVariables: new Expando(get: { -> [:] })] })
    def before = [], after = []
    void doFirst(Closure c) { before.add(c) }
    void doLast(Closure c) { after.add(c) }
}
""")
def nativeTask = nativeClass.getDeclaredConstructor().newInstance()
def listener
def tasks = new Expando()
tasks.withType = { type -> [configureEach: { c -> c(javaTask) }] }
tasks.configureEach = { c -> c(nativeTask) }
def project = new Expando(rootDir: work, tasks: tasks,
    logging: [addStandardOutputListener: { value -> listener = value }])
def fake = new Expando(gradleHomeDir: home, gradleVersion: '9.7.1')
fake.settingsEvaluated = { c -> c([settingsDir: work]) }
fake.allprojects = { c -> c(project) }
fake.rootProject = { c -> c(project) }
def binding = new Binding([gradle: fake])
new GroovyShell(loader, binding).evaluate('import org.gradle.api.GradleException\n' + new File(args[0]).text)
first.each { it.call() }
last.each { it.call() }
if (args[4] == 'native-o3') {
    nativeTask.before.each { it.call() }
    def executable = new File(compiler, 'native-image').absolutePath
    listener.onOutput("Starting process 'command '${executable}''. Working directory: ${work} Command: ${executable} -jar app.jar\n")
    nativeTask.after.each { it.call() }
}
''')
    library = libraries[-1].parent
    result = subprocess.run(
        (
            java,
            "-cp",
            str(library / "*") + os.pathsep + str(library / "plugins" / "*"),
            "groovy.ui.GroovyMain",
            str(harness),
            str(hook),
            str(capture.workspace),
            str(selected),
            str(distribution.parent),
            variant,
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(base64.b64decode(line.removeprefix(capture.marker)))
        for line in result.stdout.splitlines()
        if line.startswith(capture.marker)
    ]
    by_name = {record["toolchain"]: record for record in records}
    assert by_name["java"]["argv"] == [str(selected / "java"), "--version"]
    assert by_name["java"]["compiler"] == str(selected / "javac")
    assert by_name["java"]["output"] == 'openjdk version "25.0.1"\n'
    assert by_name["gradle"]["argv"] == [str(distribution / "gradle"), "--version"]
    if variant == "native-o3":
        assert by_name["native-image"]["argv"] == [
            str(selected / "native-image"),
            "--version",
        ]
        assert by_name["native-image"]["compiler_command"].endswith(" -jar app.jar")
        assert by_name["native-image"]["stage"] == "builder"


def test_inline_dockerfile_is_escaped_against_bake_interpolation(tmp_path):
    """Bake runs HCL interpolation over dockerfile-inline; Dockerfiles are not HCL.

    The injected Node preload keeps the caller's NODE_OPTIONS with `${NODE_OPTIONS:-}`,
    which bake rejects as a template expression. Anything else spelling `${...}` would
    be silently substituted instead, which is worse.
    """
    _capture, _, effective, _ = prepare(tmp_path, "default")

    assert effective.bake is not None
    inline = effective.bake["target"]["example"]["dockerfile-inline"]
    assert "$${NODE_OPTIONS:-}" in inline
    assert "${NODE_OPTIONS:-}" not in inline.replace("$${NODE_OPTIONS:-}", "")
    # The Dockerfile kept beside the evidence stays a real Dockerfile.
    generated = next(tmp_path.rglob(".nanolab-observation-*/Dockerfile"))
    assert "${NODE_OPTIONS:-}" in generated.read_text()
    assert "$${" not in generated.read_text()


def test_inline_dockerfile_escaping_survives_a_real_bake_parse(tmp_path):
    """The escape is only correct if buildx itself accepts it."""
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    _capture, _, effective, _ = prepare(tmp_path, "default")
    bake = tmp_path / "parse-bake.json"
    bake.write_text(json.dumps(effective.bake))

    result = subprocess.run(
        ("docker", "buildx", "bake", "-f", str(bake), "--print", "example"),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)["target"]["example"]["dockerfile-inline"]
    # Bake unescapes it back to the literal the Dockerfile parser must see.
    assert "${NODE_OPTIONS:-}" in printed

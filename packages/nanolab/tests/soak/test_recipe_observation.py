"""Run the observer against controlled external CLIs, with real owned execution."""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Timer

import pytest

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.build_executor import (
    BuildCommandError,
    OwnedBuildCommandExecutor,
)
from nanolab.tasks.soak.sources import capture_source_snapshot
from tests.soak.test_recipe import profile_data, smoke_data
from tests.soak.test_recipe_registry import artifact_fixture

GRADLE_STUB = r"""
import base64,hashlib,json,os,re,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
def option(name):
    return next(a.split('=',1)[1] for a in args if a.startswith(name+'='))
profile_path=Path(option('-Precipe'))
profile=json.loads(profile_path.read_text())
output=Path(option('-PrecipeOutput')); output.mkdir(parents=True,exist_ok=True)
docker=option('-PrecipeDocker')
text=Path(args[args.index('--init-script')+1]).read_text()
marker=re.search(r'NANOLAB_BUILD_OBSERVATION_[0-9a-f]+:',text).group()
mutation=os.environ.get('NANOLAB_TEST_MUTATION','')
if mutation=='running-cancel':
    import time
    Path(os.environ['NANOLAB_TEST_SOURCE_ROOT'],'started').touch()
    time.sleep(30)
if mutation=='stale-log': marker='NANOLAB_BUILD_OBSERVATION_'+'f'*32+':'
def frame(record):
    record.update(execution_cwd=os.getcwd(),exit_code=0,executable_sha256='a'*64,stage=None)
    if mutation=='failed-compiler' and record['toolchain']=='java': record['exit_code']=1
    print(marker+base64.b64encode(json.dumps(record).encode()).decode(),flush=True)
if mutation!='missing-gradle':
    frame(dict(toolchain='gradle',argv=['/gradle/bin/gradle','--version'],output='Gradle 9.7.1\n',effective_gradle_version='9.7.1'))
for task in (':control-plane:compileJava',':functions:java:word-stats:compileJava'):
    if mutation=='missing-role-compiler' and task==':control-plane:compileJava': continue
    if mutation=='requested-version':
        print('openjdk version "25.0.2"',flush=True);continue
    frame(dict(toolchain='java',argv=['/jdk/bin/other' if mutation=='unrelated-executable' else '/jdk/bin/java','--version'],
               output='openjdk version "25.0.2"\n',compiler='/jdk/bin/javac',compiler_sha256='a'*64,task=task))
rows=[]
if mutation=='gradle-outputs':
    cache=Path.cwd()/'platform/gradle-plugin/.gradle/buildOutputCleanup/cache.properties'
    cache.parent.mkdir(parents=True,exist_ok=True); cache.write_text('gradle.version=9.7.1')
if mutation=='added-source':
    extra=Path.cwd()/'platform/control-plane/src/main/java/Uncaptured.java'
    extra.parent.mkdir(parents=True,exist_ok=True); extra.write_text('class Uncaptured {}')
if mutation=='wrong-source': (Path.cwd()/'gradlew').write_text('changed')
if mutation=='changed-instrumentation': Path(args[args.index('--init-script')+1]).write_text('changed')
for index,(kind,item) in enumerate([('control-plane',profile['controlPlane']),*[("function",x) for x in profile['functions']]]):
    if mutation=='absent-role' and index==2: continue
    sdk='java' if kind=='control-plane' else item['sdk']
    name='control-plane' if kind=='control-plane' else item['name']
    reference=profile['registry']['repository']+'/'+item['container']['image']+':'+option('-PrecipeTag')
    source=Path.cwd()
    file=source/'functions/javascript/word-stats/Dockerfile' if sdk=='javascript' else source/'deploy/recipes/Dockerfile.jvm'
    common=['buildx','build','--builder',option('-PrecipeBuilder'),'--platform','linux/arm64','--provenance=mode=max','-t',reference,'-f',str(file),str(source)]
    subprocess.run([docker,*common],check=True)
    metadata=output/('plugin-metadata-'+str(index)+'.json')
    publish=common+['--push','--metadata-file',str(metadata)]
    if mutation=='different-assembly-input': publish+=['--build-arg','OTHER_INPUT=1']
    subprocess.run([docker,*publish],check=True)
    if mutation=='partial-publish': sys.exit(1)
    data=json.loads(metadata.read_text()); metadata.unlink()
    image=dict(reference=reference,status='published',platforms=['linux/arm64'],provenance=True,
               digest=data['containerimage.digest'],manifests={'linux/arm64':os.environ['NANOLAB_TEST_MANIFEST']})
    row=dict(kind=kind,name=name,sdk=sdk,mode='container' if sdk=='javascript' else 'jvm',image=image)
    if kind=='control-plane': row.update(variant='jvm',optimization='c2')
    rows.append(row)
revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
dirty=bool(subprocess.check_output(['git','status','--porcelain']))
report=dict(schemaVersion=2,recipe={'sha256':hashlib.sha256(profile_path.read_bytes()).hexdigest()},
            source={'revision':revision,'dirty':dirty},tag=option('-PrecipeTag'),
            modules=profile['controlPlane']['modules'],components=rows)
if mutation=='wrong-workspace':
    report['source']['revision']='a'*40
(output/'distribution.json').write_text(json.dumps(report))
"""

DOCKER_STUB = r"""
import base64,json,os,re,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2]==['buildx','inspect']:
    print('Name: test-builder\nNodes:\nName: test-builder-node\nBuildKit version: v0.27.1\nPlatforms: linux/arm64')
    sys.exit(0)
assert args[:2]==['buildx','build'],args
mutation=os.environ.get('NANOLAB_TEST_MUTATION','')
if mutation=='large-log': print('x'*8000,flush=True)
file=Path(args[args.index('-f')+1])
if 'javascript' in args[args.index('-t')+1]:
    source=next(Path(x) for x in args if x.startswith(os.environ['NANOLAB_TEST_SOURCE_ROOT']) and Path(x).is_dir())
    hook=next(source.rglob('observe-node.cjs'))
    marker=re.search(r'NANOLAB_BUILD_OBSERVATION_[0-9a-f]+:',hook.read_text()).group()
    digest=('b' if mutation=='mismatched-base' else 'a')*64
    print('#1 [build 1/8] FROM docker.io/library/node:20-alpine@sha256:'+digest,flush=True)
    print('#2 [build 4/8] RUN npm run build',flush=True)
    if mutation!='missing-node' and not (mutation=='cached-push' and '--push' in args):
        record=dict(toolchain='node',argv=['/usr/local/bin/node','--version'],output='v20.20.2\n',
                    execution_cwd='/src/sdks/javascript',exit_code=0,stage='build',
                    process_argv=['/usr/local/bin/node','/src/sdks/javascript/node_modules/.bin/tsc','-p','tsconfig.json'],executable_sha256='a'*64)
        print('#2 0.1 '+marker+base64.b64encode(json.dumps(record).encode()).decode(),flush=True)
if '--metadata-file' in args:
    metadata=dict(**{'containerimage.digest':os.environ['NANOLAB_TEST_INDEX'],
                     'containerimage.descriptor':{'digest':os.environ['NANOLAB_TEST_INDEX']},
                     'image.name':('other' if mutation=='wrong-image' else args[args.index('-t')+1]),
                     'buildx.build.ref':('other-builder/node/id' if mutation=='wrong-builder' else 'test-builder/test-builder-node/id'),
                     'buildx.build.provenance':json.loads(os.environ['NANOLAB_TEST_PREDICATE'])})
    if mutation=='wrong-invocation': metadata['buildx.build.provenance']['metadata']['buildInvocationID']='unrelated'
    if mutation=='wrong-descriptor': metadata['containerimage.descriptor']['digest']='sha256:'+'f'*64
    Path(args[args.index('--metadata-file')+1]).write_text(json.dumps(metadata))
"""


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o755)


def publication_inputs(tmp_path, monkeypatch, mutation=None, cancelled=False):
    distribution, fetch, _ = artifact_fixture(tmp_path)
    image = distribution.components[0].image
    manifest = json.loads(
        fetch("nanofaas/control-plane", "manifests", image.digest).body
    )
    attestation_digest = next(
        d["digest"] for d in manifest["manifests"] if "annotations" in d
    )
    attestation = json.loads(
        fetch("nanofaas/control-plane", "manifests", attestation_digest).body
    )
    statement = json.loads(
        fetch(
            "nanofaas/control-plane", "blobs", attestation["layers"][0]["digest"]
        ).body
    )
    source = tmp_path / "original"
    source.mkdir()
    executable(source / "gradlew", GRADLE_STUB)
    node = source / "functions/javascript/word-stats/Dockerfile"
    node.parent.mkdir(parents=True)
    node.write_text(
        "FROM node:20-alpine AS build\nWORKDIR /src\nRUN npm run build\nFROM node:20-alpine\n"
    )
    jvm = source / "deploy/recipes/Dockerfile.jvm"
    jvm.parent.mkdir(parents=True)
    jvm.write_text("FROM scratch\n")
    build = source / "platform/gradle-plugin/build.gradle"
    build.parent.mkdir(parents=True)
    build.write_text("plugins {}\n")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=source,
        check=True,
    )
    snapshot = capture_source_snapshot(
        source, tmp_path / "snapshot", max_bytes=1024 * 1024
    )
    profile = tmp_path / "recipe.yaml"
    profile.write_text(json.dumps(profile_data()))
    executable(tmp_path / "bin/docker", DOCKER_STUB)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("NANOLAB_TEST_INDEX", image.digest)
    monkeypatch.setenv("NANOLAB_TEST_MANIFEST", image.manifests["linux/arm64"])
    monkeypatch.setenv("NANOLAB_TEST_PREDICATE", json.dumps(statement["predicate"]))
    monkeypatch.setenv("NANOLAB_TEST_MUTATION", mutation or "")
    monkeypatch.setenv("NANOLAB_TEST_SOURCE_ROOT", str(tmp_path))
    event = Event()
    if cancelled:
        event.set()
    executor = OwnedBuildCommandExecutor(
        cwd=source.absolute(),
        log_dir=tmp_path / "command-logs",
        cancelled=event,
        timeout_cap_s=30,
        artifact_limit_bytes=1024 * 1024,
    )
    return (
        snapshot,
        profile,
        SoakConfig.model_validate(smoke_data()["soak"]),
        executor,
        fetch,
    )


def test_one_snapshot_one_publication_all_receipts(tmp_path, monkeypatch):
    from nanolab.tasks.soak import recipe as module

    snapshot, profile, config, executor, fetch = publication_inputs(
        tmp_path, monkeypatch
    )
    calls = []
    run = executor.run

    def record(task, **kwargs):
        calls.append(task.argv)
        return run(task, **kwargs)

    monkeypatch.setattr(executor, "run", record)
    monkeypatch.setattr(
        module,
        "fetch_local_registry",
        lambda repo, kind, ref: fetch("nanofaas/control-plane", kind, ref),
    )
    receipts = module.publish_soak_recipe(
        snapshot,
        profile,
        config,
        run_dir=tmp_path / "builds",
        tag="run-1",
        builder="test-builder",
        executor=executor,
        artifact_limit_bytes=1024 * 1024,
    )
    assert [r.role for r in receipts] == [
        "control-plane",
        "word-stats-java",
        "word-stats-javascript",
    ]
    assert {r.source_fingerprint for r in receipts} == {snapshot.fingerprint}
    assert all("@sha256:" in r.image_digest for r in receipts)
    assert all(dict(r.toolchains)["buildkit"] == "v0.27.1" for r in receipts)
    assert dict(receipts[0].toolchains)["java"] == "25.0.2"
    assert dict(receipts[2].toolchains)["node"] == "20.20.2"
    assert len(calls) == 1
    assert "publishRecipe" in calls[0]
    assert "assembleRecipe" not in calls[0]
    assert "-PrecipeBuilder=test-builder" in calls[0]
    assert all(
        Path(str(log["path"])).is_file() for receipt in receipts for log in receipt.logs
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "requested-version",
        "failed-compiler",
        "unrelated-executable",
        "stale-log",
        "absent-role",
        "missing-gradle",
        "missing-node",
        "missing-role-compiler",
        "mismatched-base",
        "wrong-image",
        "wrong-builder",
        "wrong-source",
        "added-source",
        "wrong-workspace",
        "changed-instrumentation",
        "wrong-descriptor",
        "wrong-invocation",
        "different-assembly-input",
    ],
)
def test_recipe_observation_rejects_unbound_output(tmp_path, monkeypatch, mutation):
    from nanolab.tasks.soak.recipe import publish_soak_recipe

    snapshot, profile, config, executor, fetch = publication_inputs(
        tmp_path, monkeypatch, mutation
    )
    monkeypatch.setattr(
        "nanolab.tasks.soak.recipe.fetch_local_registry",
        lambda repo, kind, ref: fetch("nanofaas/control-plane", kind, ref),
    )
    with pytest.raises(
        (ValueError, BuildCommandError),
        match=r"observ|compiler|toolchain|publication|recipe|builder|image|source|material|invocation",
    ):
        publish_soak_recipe(
            snapshot,
            profile,
            config,
            run_dir=tmp_path / "builds",
            tag="run-1",
            builder="test-builder",
            executor=executor,
            artifact_limit_bytes=1024 * 1024,
        )
    assert not list((tmp_path / "builds").glob("build-*.json"))


def test_cancelled_recipe_reaps_owned_commands(tmp_path, monkeypatch):
    from nanolab.tasks.soak.recipe import publish_soak_recipe

    snapshot, profile, config, executor, _ = publication_inputs(
        tmp_path, monkeypatch, cancelled=True
    )
    with pytest.raises(KeyboardInterrupt):
        publish_soak_recipe(
            snapshot,
            profile,
            config,
            run_dir=tmp_path / "builds",
            tag="run-1",
            builder="test-builder",
            executor=executor,
            artifact_limit_bytes=1024 * 1024,
        )
    assert executor.last_result is None
    assert not list((tmp_path / "builds").glob("build-*.json"))


def test_quota_failure_prevents_publication(tmp_path, monkeypatch):
    from nanolab.tasks.soak.recipe import publish_soak_recipe

    snapshot, profile, config, executor, _ = publication_inputs(tmp_path, monkeypatch)
    with pytest.raises(OSError, match="budget"):
        publish_soak_recipe(
            snapshot,
            profile,
            config,
            run_dir=tmp_path / "builds",
            tag="run-1",
            builder="test-builder",
            executor=executor,
            artifact_limit_bytes=128,
        )
    assert executor.last_result is None


def test_partial_publish_has_no_frozen_receipts(tmp_path, monkeypatch):
    from nanolab.tasks.soak.recipe import publish_soak_recipe

    snapshot, profile, config, executor, _ = publication_inputs(
        tmp_path, monkeypatch, "partial-publish"
    )
    with pytest.raises(BuildCommandError, match="publication failed"):
        publish_soak_recipe(
            snapshot,
            profile,
            config,
            run_dir=tmp_path / "builds",
            tag="run-1",
            builder="test-builder",
            executor=executor,
            artifact_limit_bytes=1024 * 1024,
        )
    assert executor.last_result is not None and executor.last_result.reaped
    assert not list((tmp_path / "builds").glob("build-*.json"))
    assert list((tmp_path / "builds").rglob("metadata-*.json"))


def test_cancelled_publication_reaps_running_command(tmp_path, monkeypatch):
    import time

    from nanolab.tasks.soak.recipe import publish_soak_recipe

    snapshot, profile, config, executor, _ = publication_inputs(
        tmp_path, monkeypatch, "running-cancel"
    )
    cancelled = Event()
    monkeypatch.setattr(executor, "_cancelled", cancelled)

    def cancel_started():
        for _ in range(200):
            if (tmp_path / "started").exists():
                cancelled.set()
                return
            time.sleep(0.01)

    timer = Timer(0.01, cancel_started)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            publish_soak_recipe(
                snapshot,
                profile,
                config,
                run_dir=tmp_path / "builds",
                tag="run-1",
                builder="test-builder",
                executor=executor,
                artifact_limit_bytes=1024 * 1024,
            )
    finally:
        timer.join(timeout=3)
    assert executor.last_result is not None and executor.last_result.reaped
    assert not list((tmp_path / "builds").glob("build-*.json"))


@pytest.mark.parametrize("mutation", ["cached-push", "gradle-outputs"])
def test_cached_push_uses_bound_assembly_compiler_observation(
    tmp_path, monkeypatch, mutation
):
    from nanolab.tasks.soak import recipe as module

    snapshot, profile, config, executor, fetch = publication_inputs(
        tmp_path, monkeypatch, mutation
    )
    monkeypatch.setattr(
        module,
        "fetch_local_registry",
        lambda repo, kind, ref: fetch("nanofaas/control-plane", kind, ref),
    )
    receipts = module.publish_soak_recipe(
        snapshot,
        profile,
        config,
        run_dir=tmp_path / "builds",
        tag="run-1",
        builder="test-builder",
        executor=executor,
        artifact_limit_bytes=1024 * 1024,
    )
    assert dict(receipts[2].toolchains)["node"] == "20.20.2"


@pytest.mark.parametrize("tamper", [None, "recipe", "metadata", "source"])
def test_offline_acceptance_verifies_recipe_receipt(tmp_path, monkeypatch, tamper):
    from dataclasses import asdict

    from nanolab.tasks.soak import recipe as module
    from nanolab.tasks.soak.recipe_evidence import verify_soak_recipe_receipt

    snapshot, profile, config, executor, fetch = publication_inputs(
        tmp_path, monkeypatch, "cached-push"
    )
    monkeypatch.setattr(
        module,
        "fetch_local_registry",
        lambda repo, kind, ref: fetch("nanofaas/control-plane", kind, ref),
    )
    receipts = module.publish_soak_recipe(
        snapshot,
        profile,
        config,
        run_dir=tmp_path / "builds",
        tag="run-1",
        builder="test-builder",
        executor=executor,
        artifact_limit_bytes=1024 * 1024,
    )
    if tamper:
        observer = tmp_path / "builds/recipe/observer"
        path = (
            tmp_path / "builds/recipe/recipe.yaml"
            if tamper == "recipe"
            else next(observer.glob("metadata-*.json"))
            if tamper == "metadata"
            else tmp_path / "builds/recipe/source-identity.json"
        )
        paths = (
            list(observer.glob("metadata-*.json")) if tamper == "metadata" else [path]
        )
        for item in paths:
            item.write_text(item.read_text() + " ")
    for receipt in receipts:
        if tamper:
            with pytest.raises(ValueError, match="checksum"):
                verify_soak_recipe_receipt(tmp_path, config, snapshot, asdict(receipt))
        else:
            verify_soak_recipe_receipt(tmp_path, config, snapshot, asdict(receipt))


def test_offline_acceptance_rejects_different_build_invocation(tmp_path, monkeypatch):
    from dataclasses import asdict

    from nanolab.tasks.soak import recipe as module
    from nanolab.tasks.soak.artifacts import describe_artifact
    from nanolab.tasks.soak.recipe_evidence import verify_soak_recipe_receipt

    snapshot, profile, config, executor, fetch = publication_inputs(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        module,
        "fetch_local_registry",
        lambda repo, kind, ref: fetch("nanofaas/control-plane", kind, ref),
    )
    receipts = module.publish_soak_recipe(
        snapshot,
        profile,
        config,
        run_dir=tmp_path / "builds",
        tag="run-1",
        builder="test-builder",
        executor=executor,
        artifact_limit_bytes=1024 * 1024,
    )
    for path in (tmp_path / "builds/recipe/observer").glob("metadata-*.json"):
        data = json.loads(path.read_bytes())
        data["buildx.build.provenance"]["metadata"]["buildInvocationID"] = "unrelated"
        path.write_text(json.dumps(data))
    for receipt in receipts:
        build = asdict(receipt)
        build["logs"] = tuple(
            describe_artifact(Path(log["path"])) for log in build["logs"]
        )
        with pytest.raises(ValueError, match="invocation"):
            verify_soak_recipe_receipt(tmp_path, config, snapshot, build)


@pytest.mark.parametrize(
    ("limit", "mutation"), [(50000, "large-log"), (100000, "large-log"), (65000, None)]
)
def test_recipe_quota_never_retains_excess_evidence(
    tmp_path, monkeypatch, limit, mutation
):
    from nanolab.tasks.soak import recipe as module
    from nanolab.tasks.soak.artifacts import measure_tree

    snapshot, profile, config, executor, fetch = publication_inputs(
        tmp_path, monkeypatch, mutation
    )
    monkeypatch.setattr(
        module,
        "fetch_local_registry",
        lambda repo, kind, ref: fetch("nanofaas/control-plane", kind, ref),
    )
    root = tmp_path / "builds"
    with pytest.raises(
        (OSError, ValueError, BuildCommandError), match=r"budget|quota|publication"
    ):
        module.publish_soak_recipe(
            snapshot,
            profile,
            config,
            run_dir=root,
            tag="run-1",
            builder="test-builder",
            executor=executor,
            artifact_limit_bytes=limit,
        )
    assert measure_tree(root) <= limit
    assert not (root / "frozen-images.json").exists()

"""The baseline platforms of experiment 02 on the stack VM.

Each runs the paper's one qr-code implementation through its own wrapper
(`nanofaas-paper/functions/qr-code`), installed once per run under `OPT`, and
starts inside the budget: containers through Docker's `cgroup-parent`, host
processes in the platform unit (`budget.host_service`). Versions, contracts
and quirks are the portability check's of 27 October 2026
(`nanofaas-paper/notes/evidence.md`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from nanolab.comparison.prepare import comparison_remote_command
from nanolab.config.experiment_02 import Budget, Experiment02Protocol
from nanolab.core.deployment import LOCAL_REGISTRY
from nanolab.tasks.experiment_02.budget import host_service
from nanolab.tasks.experiment_02.generator import K6Target
from nanolab.tasks.experiment_02.platforms import (
    ENTRY_PORT,
    baseline_teardown,
    record_containers,
    start_platform,
)
from nanolab.tasks.experiment_02.vm import Vm, exec_status
from nanolab.workspace.paths import bundled_assets_root

# Where the run installs platforms and wrappers on the stack; absolute, since
# host processes run as root from systemd.
OPT = PurePosixPath("/opt/nanolab-e02")
_LOCAL = f"http://127.0.0.1:{ENTRY_PORT}"
_JSON = {"Content-Type": "application/json"}


class _Baseline:
    """What every baseline shares: no metrics, no host process, no secret."""

    metrics_url: str | None = None
    host_commands: tuple[str, ...] = ()

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Return no secret."""
        return {}

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record the containers."""
        record_containers(vm, evidence)

    def teardown(self, vm: Vm) -> None:
        """Stop the platform unit and compose project, then every container."""
        baseline_teardown(vm)


# FunLess seeds a guest token in its core container at every start.
_FUNLESS_READ_GUEST = (
    "sudo docker exec e02-core-1 sed -n 's/^Guest=//p' /tmp/funless/tokens"
)
_FUNLESS_HEADERS = (
    "sh",
    "-c",
    f'printf \'{{"Authorization":"Bearer %s"}}\' "$({_FUNLESS_READ_GUEST})"',
)


@dataclass(frozen=True)
class FunLess(_Baseline):
    """FunLess 0.9.0: core, worker and Postgres in compose; the module is Wasm."""

    root: PurePosixPath = OPT / "funless"

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Bring the compose project up, create module and function, then probe."""
        bearer = f'-H "Authorization: Bearer $({_FUNLESS_READ_GUEST})"'
        return start_platform(
            vm,
            images=(),
            commands=[
                (
                    "sh",
                    "-c",
                    # A fresh signing secret per cell, generated on the VM.
                    'sudo env SECRET_KEY_BASE="$(head -c 48 /dev/urandom | base64 -w0)"'
                    f" docker compose -p e02 -f {self.root}/compose.yaml up -d",
                )
            ],
            api=f"{_LOCAL}/v1/",
            api_any_status=True,
            register={
                "commands": [
                    (
                        "sh",
                        "-c",
                        f"curl -fsS {bearer} -H 'Content-Type: application/json'"
                        f' -d \'{{"module":{{"name":"e02"}}}}\' {_LOCAL}/v1/fn',
                    ),
                    (
                        "sh",
                        "-c",
                        f"curl -fsS {bearer} -F name=qr"
                        f" -F code=@{self.root}/qr_funless.wasm {_LOCAL}/v1/fn/e02",
                    ),
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
            headers_command=_FUNLESS_HEADERS,
        )

    def target(self, host: str) -> K6Target:
        """Invoke through the core; the PNG is in `data`."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/v1/fn/e02/qr", _JSON, "json-base64", "data"
        )

    def body(self, payload: bytes) -> bytes:
        """Send the payload as the invocation's `args`."""
        return json.dumps({"args": json.loads(payload)}, separators=(",", ":")).encode()

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Read this deployment's guest token, for the generator alone."""
        return json.loads(
            comparison_remote_command(vm.provider, vm.request, _FUNLESS_HEADERS)
        )


@dataclass(frozen=True)
class Spin(_Baseline):
    """Spin 4.2.5 serving the qr-code component; one host process."""

    root: PurePosixPath = OPT / "spin"
    host_commands: tuple[str, ...] = ("spin",)

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Run `spin up` in the platform unit and probe the component."""
        return start_platform(
            vm,
            images=(),
            commands=[
                host_service(
                    (
                        str(self.root / "spin"),
                        "up",
                        "--listen",
                        f"0.0.0.0:{ENTRY_PORT}",
                        "-f",
                        str(self.root / "spin.toml"),
                    )
                )
            ],
            # The component answers any request; a GET without a body is a 400.
            api=f"{_LOCAL}/",
            api_any_status=True,
            register=None,
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the component's answer: the PNG as a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Send the payload itself."""
        return payload


@dataclass(frozen=True)
class TinyFaas(_Baseline):
    """tinyFaaS eff00d0: its manager on the host, the function in Docker.

    The `binary` runtime forks `fn.sh` per request; HTTP on the entry port,
    CoAP and gRPC on their default ports, unused.
    """

    root: PurePosixPath = OPT / "tinyfaas"
    # `comm` keeps 15 characters of a process name.
    host_commands: tuple[str, ...] = ("tinyfaas-linux-", "rproxy-linux-am")

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Run the manager in the platform unit, upload the function, probe it."""
        upload = (
            "import json, sys; d = json.load(open(sys.argv[1]));"
            f' d["threads"] = {protocol.fixed_concurrency};'
            " sys.stdout.write(json.dumps(d))"
        )
        return start_platform(
            vm,
            images=(),
            commands=[
                host_service(
                    ("sh", "-c", f"cd {self.root} && exec ./tinyfaas-linux-amd64"),
                    # CoAP and gRPC keep their defaults: rproxy at eff00d0
                    # exits with fewer than two protocols
                    # (cmd/rproxy/main.go:22).
                    env={"http_PORT": str(ENTRY_PORT)},
                )
            ],
            api="http://127.0.0.1:8080/list",
            api_any_status=True,
            register={
                "commands": [
                    (
                        "sh",
                        "-c",
                        # A failed upload may leave handlers tinyFaaS no
                        # longer tracks: never upload over them.
                        '[ -z "$(docker ps -aq --filter label=tinyfaas-function=qr)" ]'
                        " || { echo 'handlers of a failed upload remain' >&2;"
                        " exit 1; }\n"
                        f"python3 -c '{upload}' {self.root}/upload.json"
                        " | curl -fsS --data-binary @- http://127.0.0.1:8080/upload",
                    )
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the function's stdout: the PNG as a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/qr", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Send the payload itself, the function's stdin."""
        return payload


ETCD = "gcr.io/etcd-development/etcd:v3.5.13"
SERVERLEDGE_IMAGE = f"{LOCAL_REGISTRY}/qr-serverledge:e02"


@dataclass(frozen=True)
class Serverledge(_Baseline):
    """Serverledge 45419f5 with etcd; the function is a `custom` image.

    The container pool gets the cell's budget, so the platform admits no more
    than the budget holds.
    """

    budget: Budget
    root: PurePosixPath = OPT / "serverledge"
    image: str = SERVERLEDGE_IMAGE
    host_commands: tuple[str, ...] = ("serverledge",)

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start etcd and Serverledge, register the function, probe it."""
        conf = self.root / "serverledge-conf.yaml"
        settings = (
            f"api:\\n  port: {ENTRY_PORT}\\ncontainer:\\n  pool:\\n"
            f"    memory: {self.budget.memory_mib}\\n    cpus: {self.budget.cpus:g}\\n"
        )
        return start_platform(
            vm,
            images=(ETCD, self.image),
            commands=[
                ("sudo", "sh", "-c", f"printf '{settings}' > {conf}"),
                (
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    "e02-etcd",
                    "-p",
                    "127.0.0.1:2379:2379",
                    ETCD,
                    "/usr/local/bin/etcd",
                    "--listen-client-urls",
                    "http://0.0.0.0:2379",
                    "--advertise-client-urls",
                    "http://0.0.0.0:2379",
                ),
                host_service((str(self.root / "bin" / "serverledge"), str(conf))),
            ],
            api=f"{_LOCAL}/status",
            api_any_status=True,
            register={
                "commands": [
                    (
                        str(self.root / "bin" / "serverledge-cli"),
                        "-P",
                        str(ENTRY_PORT),
                        "create",
                        "--function",
                        "qr",
                        "--memory",
                        "512",
                        "--runtime",
                        "custom",
                        "--custom_image",
                        self.image,
                        "--max_concurrency",
                        str(protocol.fixed_concurrency),
                    )
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the PNG from `Result`, a JSON string holding its base64."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/invoke/qr",
            _JSON,
            "json-string-base64",
            "Result",
        )

    def body(self, payload: bytes) -> bytes:
        """Send the payload as the invocation's `Params`."""
        return json.dumps(
            {"Params": json.loads(payload)}, separators=(",", ":")
        ).encode()


FNSERVER = (
    "fnproject/fnserver@sha256:"
    "16dedb58b2f1be5e02d7a851a66041ea97076801cbdb09cdfee0100296a68686"
)
FN_IMAGE = f"{LOCAL_REGISTRY}/qr-fn:e02"
# Idempotent: a retried registration finds what an earlier attempt created.
_FN_REGISTER = f"""
import json, urllib.request
api = "{_LOCAL}/v2"
def call(path, body=None):
    request = urllib.request.Request(api + path, method="POST" if body else "GET",
        data=json.dumps(body).encode() if body else None,
        headers={{"Content-Type": "application/json"}})
    return json.load(urllib.request.urlopen(request, timeout=30))
def ensure(kind, query, body):
    found = call(f"/{{kind}}?{{query}}")["items"]
    return found[0]["id"] if found else call(f"/{{kind}}", body)["id"]
app = ensure("apps", "name=e02", {{"name": "e02"}})
fn = ensure("fns", f"app_id={{app}}&name=qr", {{"name": "qr", "app_id": app,
    "image": "{FN_IMAGE}", "memory": 256, "timeout": TIMEOUT}})
ensure("triggers", f"app_id={{app}}&name=qr", {{"name": "qr", "app_id": app,
    "fn_id": fn, "type": "http", "source": "/qr"}})
"""


@dataclass(frozen=True)
class Fn(_Baseline):
    """Fn 0.3.770 in single-server mode, starting function containers itself."""

    root: PurePosixPath = OPT / "fn"
    image: str = FN_IMAGE

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start fnserver, create app, function and trigger, probe the trigger."""
        iofs = self.root / "iofs"
        register = _FN_REGISTER.replace(
            "TIMEOUT", str(protocol.http_timeout_seconds)
        ).replace(FN_IMAGE, self.image)
        return start_platform(
            vm,
            images=(FNSERVER, self.image),
            commands=[
                (
                    "docker",
                    "run",
                    "-d",
                    "--privileged",
                    "--name",
                    "e02-fnserver",
                    "-v",
                    f"{iofs}:/iofs",
                    "-e",
                    f"FN_IOFS_DOCKER_PATH={iofs}",
                    "-e",
                    "FN_IOFS_PATH=/iofs",
                    "-v",
                    "/var/run/docker.sock:/var/run/docker.sock",
                    "-p",
                    f"{ENTRY_PORT}:8080",
                    FNSERVER,
                )
            ],
            api=f"{_LOCAL}/version",
            register={"commands": [("python3", "-c", register)]},
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Invoke through the HTTP trigger; the PNG is a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/t/e02/qr", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Send the payload itself."""
        return payload


# ---------------------------------------------------------------- installation

SPIN_VERSION = "v4.2.5"
TINYFAAS_COMMIT = "eff00d060f6f6e96bf8f35475a0e9a8e6e42de32"
SERVERLEDGE_COMMIT = "45419f50675aa7c8cc3813f626e17f6c28c8a641"
# ponytail: tags, not digests; install records the digest each one resolved to.
GOLANG = "golang:1.24"
_FUNLESS_IMAGES = (
    "postgres:16",
    "ghcr.io/funlessdev/core@sha256:"
    "c75155af88585663d687b478ef0e69e46668e26c3b05d0cab59bc3583fa65954",
    "ghcr.io/funlessdev/worker@sha256:"
    "bb774f588228c704db6a4677e2915de20ce3ae2731ada0b56bd927188f9acd41",
)
# What an installation pins, per baseline: a change here is another identity.
_PINS = {
    "funless": " ".join(_FUNLESS_IMAGES),
    "spin": SPIN_VERSION,
    "tinyfaas": TINYFAAS_COMMIT,
    "serverledge": f"{SERVERLEDGE_COMMIT} {GOLANG} {ETCD}",
    "fn": FNSERVER,
}
# Images whose digest is recorded, the tags among them included.
_PULLED = {
    "funless": _FUNLESS_IMAGES,
    "serverledge": (ETCD, GOLANG),
    "fn": (FNSERVER,),
}
_GIT = {
    "tinyfaas": "https://github.com/OpenFogStack/tinyFaaS",
    "serverledge": "https://github.com/serverledge-faas/serverledge",
}
# Zip fn.sh and qr-stdio into tinyFaaS's upload request; threads are set per cell.
_TINYFAAS_UPLOAD = (
    "import base64, io, json, sys, zipfile; out = io.BytesIO()\n"
    'with zipfile.ZipFile(out, "w") as z:\n'
    '    z.write(sys.argv[1], "fn.sh"); z.write(sys.argv[2], "qr-stdio")\n'
    'body = {"name": "qr", "env": "binary", "threads": 1,'
    ' "zip": base64.b64encode(out.getvalue()).decode()}\n'
    'json.dump(body, open(sys.argv[3], "w"))'
)


def _image(key: str, identity: str) -> str:
    """Return the registry tag of a baseline's function image for one identity."""
    return f"{LOCAL_REGISTRY}/qr-{key}:{identity}"


def _install_script(
    key: str, source: PurePosixPath, root: PurePosixPath, identity: str
) -> str:
    """Return the root shell script that installs one baseline into `root`.

    One command per line, run under `set -e`: a failed step stops the script.
    """
    release = source / "target" / "release"
    if key == "funless":
        compose = (
            bundled_assets_root() / "experiment02" / "funless-compose.yaml"
        ).read_text()
        return "\n".join(
            (
                f"cat > {root}/compose.yaml <<'NANOLAB_EOF'\n{compose}NANOLAB_EOF",
                f"cp {source}/target/wasm32-wasip1/release/qr_funless.wasm {root}/",
                *(f"docker pull -q {image}" for image in _FUNLESS_IMAGES),
            )
        )
    if key == "spin":
        tarball = f"spin-{SPIN_VERSION}-static-linux-amd64.tar.gz"
        url = f"https://github.com/spinframework/spin/releases/download/{SPIN_VERSION}"
        return "\n".join(
            (
                f"cd {root}",
                f"curl -fsSLO {url}/{tarball}",
                f"curl -fsSLO {url}/checksums-{SPIN_VERSION}.txt",
                f"grep ' {tarball}$' checksums-{SPIN_VERSION}.txt > {tarball}.sha256",
                f"sha256sum -c {tarball}.sha256",
                f"tar xzf {tarball} spin",
                f"cp {source}/target/wasm32-wasip2/release/qr_spin.wasm"
                f" {source}/spin/spin.toml {root}/",
            )
        )
    if key == "tinyfaas":
        return "\n".join(
            (
                "apt-get install -y -q make golang-go >/dev/null",
                f"git clone -q {_GIT['tinyfaas']} {root}/src",
                f"cd {root}/src",
                f"git checkout -q {TINYFAAS_COMMIT}",
                "make build >/dev/null",
                f"mv tinyfaas-linux-amd64 {root}/",
                f"python3 -c '{_TINYFAAS_UPLOAD}' {source}/tinyfaas/fn.sh"
                f" {release}/qr-stdio {root}/upload.json",
            )
        )
    if key == "serverledge":
        image = _image(key, identity)
        return "\n".join(
            (
                f"git clone -q {_GIT['serverledge']} {root}/src",
                f"cd {root}/src",
                f"git checkout -q {SERVERLEDGE_COMMIT}",
                f"docker run --rm -v $PWD:/src -w /src {GOLANG}"
                " make serverledge serverledge-cli >/dev/null",
                f"mkdir -p {root}/bin {root}/image",
                f"cp bin/serverledge bin/serverledge-cli {root}/bin/",
                "docker build -q -t serverledge-base:45419f5"
                " -f images/base-alpine/Dockerfile . >/dev/null",
                f"cp {source}/serverledge/Dockerfile {source}/serverledge/run.sh"
                f" {release}/qr-stdio {root}/image/",
                f"docker build -q -t {image} {root}/image >/dev/null",
                f"docker push -q {image} >/dev/null",
                f"docker pull -q {ETCD}",
            )
        )
    if key == "fn":
        image = _image(key, identity)
        return "\n".join(
            (
                f"mkdir -p {root}/iofs {root}/image",
                f"docker pull -q {FNSERVER}",
                f"cp {source}/fn/Dockerfile {release}/qr-fn {root}/image/",
                f"docker build -q -t {image} {root}/image >/dev/null",
                f"docker push -q {image} >/dev/null",
            )
        )
    raise ValueError(f"unknown experiment-02 baseline: {key}")


def _observe(stack: Vm, key: str, root: PurePosixPath) -> str:
    """Return the versions an installation actually holds, for the evidence."""
    probes = [
        f"docker image inspect --format '{{{{index .RepoDigests 0}}}}' {image}"
        for image in _PULLED.get(key, ())
    ]
    if key == "spin":
        probes.append(f"{root}/spin --version")
    if key in _GIT:
        probes.append(f"git -C {root}/src rev-parse HEAD")
    if not probes:
        return ""
    return exec_status(stack, ("sh", "-c", "; ".join(probes))).stdout


def baseline_identity(
    key: str, *, artifacts: Mapping[str, str], workload_tree: str
) -> str:
    """Return the identity of one baseline's installation.

    It hashes everything the installation is made of: what the baseline pins,
    its install script (the FunLess compose file included), the workload tree
    the wrapper files are copied from, and the built artifacts' sha256.
    """
    script = _install_script(
        key, PurePosixPath("SOURCE"), PurePosixPath("ROOT"), "IDENTITY"
    )
    return hashlib.sha256(
        json.dumps(
            [key, _PINS[key], script, workload_tree, dict(artifacts)], sort_keys=True
        ).encode()
    ).hexdigest()[:16]


def install_baselines(
    stack: Vm,
    keys: Sequence[str],
    *,
    source: PurePosixPath,
    artifacts: Mapping[str, str],
    workload_tree: str,
) -> dict[str, dict[str, str]]:
    """Install each selected baseline once per identity, beside any other.

    Each identity (`baseline_identity`) has its own directory and image tag,
    so another run on the same VM never changes what this one installed. The
    marker is written only after the whole script succeeded. Everything
    happens before any cell.
    """
    installed: dict[str, dict[str, str]] = {}
    for key in keys:
        identity = baseline_identity(
            key, artifacts=artifacts, workload_tree=workload_tree
        )
        root = OPT / key / identity
        marker = root / ".installed"
        if exec_status(stack, ("test", "-f", str(marker))).return_code:
            script = _install_script(key, source, root, identity)
            _ = comparison_remote_command(
                stack.provider,
                stack.request,
                (
                    "sudo",
                    "sh",
                    "-c",
                    f"set -e\nrm -rf {root}\nmkdir -p {root}\n{script}",
                ),
            )
            _ = comparison_remote_command(
                stack.provider, stack.request, ("sudo", "touch", str(marker))
            )
        installed[key] = {
            "identity": identity,
            "root": str(root),
            "image": _image(key, identity) if key in ("serverledge", "fn") else "",
            "observed": _observe(stack, key, root),
        }
    return installed


def ensure_baselines(
    stack: Vm,
    keys: Sequence[str],
    *,
    source: PurePosixPath,
    artifacts: Mapping[str, str],
    workload_tree: str,
    record: Path,
) -> dict[str, dict[str, str]]:
    """Install the baselines for a run; on a resume, require the ones recorded.

    `record` keeps what the run's first installation observed. A resume whose
    inputs make another identity is refused before anything is installed, one
    whose installation no longer holds what was observed after.
    """
    recorded = json.loads(record.read_text()) if record.is_file() else None
    changed = (
        f"the baselines' installation changed since {record}; start a new run directory"
    )
    if recorded is not None:
        identities = {
            key: baseline_identity(
                key, artifacts=artifacts, workload_tree=workload_tree
            )
            for key in keys
        }
        if identities != {key: entry["identity"] for key, entry in recorded.items()}:
            raise RuntimeError(changed)
    installed = install_baselines(
        stack, keys, source=source, artifacts=artifacts, workload_tree=workload_tree
    )
    if recorded is not None and recorded != installed:
        raise RuntimeError(changed)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(installed, indent=2) + "\n")
    return installed

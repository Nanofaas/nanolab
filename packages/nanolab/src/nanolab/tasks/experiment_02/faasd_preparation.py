"""Prepare the shared faasd image on stack, install containerd on its own VM."""

from __future__ import annotations

import hashlib
import json
import shlex
import tempfile
from pathlib import Path, PurePosixPath

from nanolab.comparison.prepare import (
    comparison_remote_command,
    comparison_transfer_from,
    comparison_transfer_to,
)
from nanolab.tasks.experiment_02.faasd import teardown_argv
from nanolab.tasks.experiment_02.vm import Vm
from nanolab.workspace.paths import bundled_assets_root

FAASD_VERSION = "0.19.7"
FAASD_COMMIT = "a2254ca1ff26df107a9dba43f391b5cfcb5c145f"
CONTAINERD_VERSION = "1.7.27"
CNI_VERSION = "0.9.1"


def faasd_identity(*, artifact_sha256: str, workload_tree: str) -> str:
    """Bind installation scripts and platform versions to the shared workload."""
    content = [
        FAASD_COMMIT,
        CONTAINERD_VERSION,
        CNI_VERSION,
        artifact_sha256,
        workload_tree,
        Path(__file__).read_text(),
    ]
    for name in ("experiment02_containerd_namespace.sh",):
        path = bundled_assets_root() / "diagnostics" / name
        content.append(path.read_text())
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def require_record(record: Path, identity: str) -> dict[str, str] | None:
    """Refuse changed identities before any installation or image publication."""
    if not record.is_file():
        return None
    saved = json.loads(record.read_text())
    if saved.get("identity") != identity:
        raise ValueError("faasd installation changed; use a new run directory")
    return saved


# Installation runs only on the dedicated Azure VM; no latest-release lookup.
INSTALL = r"""set -eu
root="$1"
namespace_script="$2"
apt-get update -q >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  curl git skopeo runc bridge-utils iptables python3-yaml >/dev/null
mkdir -p "$root/bin" /opt/cni/bin /usr/local/libexec
cd "$root"
curl -fLsS --retry 3 --max-time 120 -O https://github.com/containerd/containerd/releases/download/v1.7.27/containerd-1.7.27-linux-amd64.tar.gz
curl -fLsS --retry 3 --max-time 120 -O https://github.com/containerd/containerd/releases/download/v1.7.27/containerd-1.7.27-linux-amd64.tar.gz.sha256sum
sha256sum -c containerd-1.7.27-linux-amd64.tar.gz.sha256sum
tar -C /usr/local -xzf containerd-1.7.27-linux-amd64.tar.gz
curl -fLsS --retry 3 --max-time 120 -O https://github.com/containernetworking/plugins/releases/download/v0.9.1/cni-plugins-linux-amd64-v0.9.1.tgz
curl -fLsS --retry 3 --max-time 120 -O https://github.com/containernetworking/plugins/releases/download/v0.9.1/cni-plugins-linux-amd64-v0.9.1.tgz.sha256
sha256sum -c cni-plugins-linux-amd64-v0.9.1.tgz.sha256
tar -C /opt/cni/bin -xzf cni-plugins-linux-amd64-v0.9.1.tgz
curl -fLsS --retry 3 --max-time 120 \
  https://github.com/openfaas/faasd/releases/download/0.19.7/faasd -o bin/faasd
curl -fLsS --retry 3 --max-time 120 -O https://github.com/openfaas/faasd/releases/download/0.19.7/faasd.sha256
sha256sum -c faasd.sha256
install -m755 bin/faasd /usr/local/bin/faasd
if [ ! -d source/.git ]; then
  git clone -q --branch 0.19.7 --depth 1 https://github.com/openfaas/faasd source
fi
[ "$(git -C source rev-parse HEAD)" = a2254ca1ff26df107a9dba43f391b5cfcb5c145f ]
install -m755 "$namespace_script" /usr/local/libexec/nanolab-containerd-namespace
mkdir -p /etc/systemd/system/faasd.service.d \
  /etc/systemd/system/faasd-provider.service.d
for slice in nanolab nanolab-sut nanolab-infra nanolab-infra-observers; do
  printf '[Slice]\n' > "/etc/systemd/system/$slice.slice"
done
cat > /etc/systemd/system/containerd.service <<'UNIT'
[Unit]
Description=Containerd in a delegated measurement cgroup namespace
[Service]
ExecStart=/usr/local/libexec/nanolab-containerd-namespace
Slice=nanolab-sut.slice
Delegate=yes
KillMode=control-group
[Install]
WantedBy=multi-user.target
UNIT
swapoff -a
systemctl daemon-reload
systemctl restart containerd
cd source
/usr/local/bin/faasd install >/dev/null 2>&1
systemctl stop faasd faasd-provider
for service in faasd faasd-provider; do
  printf '[Service]\nSlice=nanolab-sut.slice\n' \
    > "/etc/systemd/system/$service.service.d/nanolab.conf"
done
python3 - "$root/source/docker-compose.yaml" <<'COMPOSE'
import sys, yaml
with open(sys.argv[1]) as stream:
    compose = yaml.safe_load(stream)
compose['services']['gateway']['ports'] = ['30080:8080']
compose['services']['registry'] = {
    'image': 'docker.io/library/registry:2',
    'ports': ['127.0.0.1:5000:5000'],
    'volumes': [{'type': 'bind', 'source': './registry',
                 'target': '/var/lib/registry'}],
}
with open('/var/lib/faasd/docker-compose.yaml', 'w') as stream:
    yaml.safe_dump(compose, stream)
COMPOSE
mkdir -p /var/lib/faasd/registry
systemctl daemon-reload
systemctl start faasd-provider faasd
"""


def _verification(vm: Vm, image: str) -> str:
    tag = image.rsplit(":", 1)[-1]
    link = (
        PurePosixPath(
            "/var/lib/faasd/registry/docker/registry/v2/repositories/qr-faasd"
        )
        / "_manifests/tags"
        / tag
        / "current/link"
    )
    script = r"""set -eu
sha256sum /usr/local/bin/faasd /usr/local/bin/containerd "$(command -v runc)" \
  /usr/local/libexec/nanolab-containerd-namespace \
  /opt/cni/bin/bridge /opt/cni/bin/host-local \
  /opt/cni/bin/firewall /opt/cni/bin/loopback \
  /etc/cni/net.d/10-openfaas.conflist \
  /etc/systemd/system/containerd.service /etc/systemd/system/nanolab-sut.slice \
  /var/lib/faasd/docker-compose.yaml %LINK%
units=$(systemctl cat containerd faasd faasd-provider)
printf '%s' "$units" | sha256sum
containerd --version
runc --version
images=$(ctr -n openfaas images list)
printf '%s\n' "$images" | awk 'NR>1 {print $1 " " $3}' | sort
"""
    return comparison_remote_command(
        vm.provider,
        vm.request,
        (
            "sudo",
            "sh",
            "-c",
            script.replace("%LINK%", shlex.quote(str(link))),
        ),
    ).strip()


def ensure_faasd(
    stack: Vm,
    native: Vm,
    *,
    source: PurePosixPath,
    artifact_sha256: str,
    workload_tree: str,
    remote_root: PurePosixPath,
    record: Path,
) -> dict[str, str]:
    """Build once on stack, import into faasd's registry, and verify on resume."""
    identity = faasd_identity(
        artifact_sha256=artifact_sha256, workload_tree=workload_tree
    )
    image = f"127.0.0.1:5000/qr-faasd:{identity}"
    saved = require_record(record, identity)
    if saved is not None:
        if _verification(native, saved["image"]) != saved["verification"]:
            raise ValueError("faasd installation drifted; use a new run directory")
        return saved
    archive = remote_root / f"faasd-{identity}.tar"
    stack_archive = source.parent / archive.name
    context = source / "faasd"
    build = (
        f"cp {shlex.quote(str(source / 'target/release/qr-stdio'))} "
        f"{shlex.quote(str(context / 'qr-stdio'))} && "
        f"docker build -t {shlex.quote(image)} {shlex.quote(str(context))} && "
        f"docker save -o {shlex.quote(str(stack_archive))} {shlex.quote(image)}"
    )
    comparison_remote_command(stack.provider, stack.request, ("sh", "-c", build))
    with tempfile.TemporaryDirectory() as scratch:
        local = Path(scratch) / "faasd.tar"
        comparison_transfer_from(stack.provider, stack.request, stack_archive, local)
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        comparison_remote_command(
            native.provider, native.request, ("mkdir", "-p", str(remote_root))
        )
        comparison_transfer_to(native.provider, native.request, local, archive)
    namespace = (
        bundled_assets_root() / "diagnostics/experiment02_containerd_namespace.sh"
    )
    remote_script = remote_root / namespace.name
    comparison_transfer_to(native.provider, native.request, namespace, remote_script)
    checked = (
        f"printf '%s  %s\\n' {digest} {shlex.quote(str(archive))} | sha256sum -c -"
    )
    comparison_remote_command(native.provider, native.request, ("sh", "-c", checked))
    comparison_remote_command(
        native.provider,
        native.request,
        (
            "sh",
            "-c",
            '! command -v docker && [ "$(uname -m)" = x86_64 ] && '
            '[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ]',
        ),
    )
    install_root = f"/opt/nanolab-e02/faasd/{identity}"
    comparison_remote_command(
        native.provider,
        native.request,
        (
            "sudo",
            "sh",
            "-c",
            INSTALL,
            "faasd-install",
            install_root,
            str(remote_script),
        ),
    )
    comparison_remote_command(native.provider, native.request, teardown_argv())
    comparison_remote_command(
        native.provider,
        native.request,
        (
            "sudo",
            "systemctl",
            "start",
            "faasd-provider",
            "faasd",
        ),
    )
    comparison_remote_command(
        native.provider,
        native.request,
        (
            "sh",
            "-c",
            "curl -fsS --retry 30 --retry-all-errors --retry-delay 1 "
            "--max-time 2 http://127.0.0.1:5000/v2/ >/dev/null",
        ),
    )
    comparison_remote_command(
        native.provider,
        native.request,
        (
            "sudo",
            "skopeo",
            "copy",
            "--dest-tls-verify=false",
            f"docker-archive:{archive}",
            f"docker://{image}",
        ),
    )
    comparison_remote_command(
        native.provider,
        native.request,
        (
            "sudo",
            "ctr",
            "-n",
            "openfaas-fn",
            "images",
            "pull",
            "--plain-http",
            image,
        ),
    )
    saved = {
        "identity": identity,
        "root": "/var/lib/faasd",
        "image": image,
        "verification": _verification(native, image),
        "archiveSha256": digest,
        "faasdCommit": FAASD_COMMIT,
        "containerdVersion": CONTAINERD_VERSION,
    }
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(saved, indent=2) + "\n")
    return saved

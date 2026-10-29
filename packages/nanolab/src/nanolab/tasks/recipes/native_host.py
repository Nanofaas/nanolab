"""Prepare experiment 02's pinned Community native-image toolchain on its VM."""

from pathlib import Path, PurePosixPath

GRAALVM_HOME = "/opt/graalvm"
_BOOTSTRAP = rf"""test "$(uname -m)" = x86_64
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q build-essential zlib1g-dev >/dev/null
if [ ! -x {GRAALVM_HOME}/bin/native-image ]; then
  sh "$1/scripts/install-graalvm.sh" amd64 "$2" "$3" community
fi
version="$({GRAALVM_HOME}/bin/native-image --version)"
printf '%s\n' "$version"
java=$(printf '%s\n' "$version" | head -n1 | cut -d' ' -f2)
release=$(printf '%s\n' "$version" |
  sed -n 's/.*GraalVM CE \([^+ ]*\).*/\1/p' | head -n1)
if [ "$java" != "$3" ] || [ "$release" != "$2" ]; then
  echo 'native toolchain differs from captured source pins' >&2
  exit 1
fi
gcc --version
dpkg-query -W build-essential gcc libc6-dev zlib1g-dev
sha256sum {GRAALVM_HOME}/bin/native-image
"""


def native_host_command(source: Path, remote_source: PurePosixPath) -> tuple[str, ...]:
    """Use captured source pins; never compile or install on the operator host."""
    properties = {}
    for line in (source / "gradle.properties").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            properties[key.strip()] = value.strip()
    try:
        release = properties["graalvmVersion"]
        java = properties["graalvmJavaVersion"]
        if not release or not java:
            raise KeyError("empty pin")
    except KeyError as error:
        raise ValueError(
            "native builds require graalvmVersion and graalvmJavaVersion "
            "in captured source"
        ) from error
    return (
        "sudo",
        "sh",
        "-eu",
        "-c",
        _BOOTSTRAP,
        "native-builder",
        str(remote_source),
        release,
        java,
    )

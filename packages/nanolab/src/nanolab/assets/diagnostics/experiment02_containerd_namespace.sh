#!/bin/sh
set -eu
if [ "${1:-}" != inner ]; then
    NANOLAB_CGROUP_DIR="/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)"
    export NANOLAB_CGROUP_DIR
    exec unshare --cgroup --mount --propagation private "$0" inner
fi
mkdir -p "$NANOLAB_CGROUP_DIR/daemon"
printf '%s\n' "$$" > "$NANOLAB_CGROUP_DIR/daemon/cgroup.procs"
printf '+cpu +memory +pids\n' > "$NANOLAB_CGROUP_DIR/cgroup.subtree_control"
umount /sys/fs/cgroup
mount -t cgroup2 cgroup2 /sys/fs/cgroup
exec /usr/local/bin/containerd

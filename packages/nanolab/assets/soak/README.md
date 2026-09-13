# Local Docker diagnostic helper integration

This implements the diagnostic part of P24, not a smoke substitute or a P24
execution result. Main owns image building, permission decisions, preparation,
capture and teardown. No image is pulled or built by this provisioner.

## Published runtime API

```python
from pathlib import Path
from nanolab.tasks.soak.diagnostic_helper import (
    DockerHelperSpec,
    LocalDockerDiagnosticProvisioner,
)

# target is the existing Target, with the full container ID, HOST init PID,
# Docker State.StartedAt and target image RepoDigest. output_root already exists.
spec = DockerHelperSpec(
    target=target,
    helper_image=operator_resolved_helper_repo_digest,
    owner_label=run_owner_label,
    owner_value=run_id,
    uid=target_effective_uid,
    gid=target_effective_gid,
    output_root=output_root.resolve(),
    quota_bytes=per_capture_quota_bytes,  # page aligned, <= adapter capture budget
    helper_memory_bytes=helper_memory_limit,  # strictly above tmpfs quota
    allow_target_stop_on_cancel=True,  # explicit owner permission, no default
    target_tmp_volume=owned_tmp_volume_name,  # already mounted on target /tmp
)
prepared = LocalDockerDiagnosticProvisioner(
    assets_dir=Path("packages/nanolab/assets/soak"),
).prepare(spec, timeout_s=60)
adapter = prepared.adapter(
    budget=shared_diagnostic_budget,
    natural_checkpoint=completed_natural_checkpoint,
    max_capture_bytes=per_capture_budget_bytes,
)
# Adapter.capture requires fresh owned output directories and preserves the
# existing natural-drain checkpoint and aggregate reservation gates.
gc_receipt = adapter.capture(target, "gc", gc_output_dir, gc_timeout_s)
dump_receipt = adapter.capture(target, "heap_dump", dump_output_dir, dump_timeout_s)
# Main calls prepared.cancel() on an abort DURING capture; ordinary teardown:
prepared.close()
```

The returned handle also exposes `.executor`, `.receipt`, `.helper_id`, and
`.quota_bytes`. Calling prepare performs real Docker operations; construction and
import do not. Native targets are intentionally unsupported.

## PSS API for Zeno runtime

Use `prepared.read_memory(timeout_s=5)` on a diagnostic handle. For an observer
that only needs procfs, including native targets, use a separate persistent
memory helper:

```python
from dataclasses import replace

memory_spec = replace(
    spec,
    target=observed_target,
    uid=observed_uid,
    gid=observed_gid,
    memory_only=True,
    allow_target_stop_on_cancel=False,
)
reader = provisioner.prepare_memory(memory_spec, timeout_s=30)
first = reader.initial_sample
sample = reader.read_memory(timeout_s=5)
reader.close()
```

Create a spec directly with `memory_only=True` for native targets. A memory-only
helper has no host mounts and does not need a Node preload, writable target tmp,
or runtime attachment. It uses the same pinned worker image. The bounded helper
tmpfs/memory fields remain required; e.g. a 4096-byte out quota with a 64-MiB
helper cap is sufficient for a procfs reader, independently of diagnostic quotas.
It reads `/proc/1/status` and `/proc/1/smaps_rollup` via Docker exec as the target
UID, not via inaccessible host-UID1000 smaps. It never stops the target, including
on timeout: cancellation removes only the exact helper and its exec reader.

Response schema is `nanolab-soak-memory-helper-v1`, with `target`, `before`,
`after`, `status`, `smaps_rollup`, `errors`, `source`, `started_s`, and `ended_s`.
Both identity objects carry UID/GID, PID namespace identities, target namespace
PID 1 and start ticks. The text fields are verbatim procfs contents. A failed read
is `null` with a per-file error string; parse successful text using the existing
procfs parser and report missing PSS as unavailable. Never substitute RSS, a
Docker working-set estimate, or zero. Reads check identity before and after.

Preparation retains the first observed sample, including any explicit read
failure, in `observations.json`. Host namespace readlink permission failures are
not interpreted as a namespace match; matching same-UID worker observations and
Docker's exact PID-namespace container binding provide that evidence. If target
capabilities or non-dumpability also prevent same-UID access, availability remains
unresolved. Drop target capabilities where possible; no extra helper capability
is added. Reads serialize with diagnostics on a shared helper; a busy helper
produces a timeout rather than fabricated data.

## Exact runtime requirements

- Local Linux Docker Unix socket, numeric UID/GID and accessible host procfs.
  P24 defaults are architecture `arm64`, `/usr/bin/docker` and
  `unix:///var/run/docker.sock`. Override the executable/socket explicitly if needed.
- Targets must be the container's init process in its own PID namespace, with
  full IDs, matching run ownership labels, immutable RepoDigests and
  `restart: "no"`. Do not use an init wrapper that makes Java/Node a child PID.
- No privileged containers, SYS_ADMIN, or host namespaces. Helper creation uses
  `--pid container:<exact-id>`, `--network none`, `--cap-drop ALL`, matching UID/GID,
  no-new-privileges, read-only root, private IPC/cgroup namespaces, a PID limit,
  and separately specified helper memory/swap limits.
- Diagnostic targets need an existing owned Docker volume mounted at `/tmp`.
  Main creates it BEFORE the target: driver `local`, scope `local`, options
  `type=tmpfs`, `device=tmpfs`, and
  `o=size=16777216,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev` (use the
  actual target UID/GID). Size must be explicit decimal bytes, page-aligned,
  between 4096 and 1073741824; duplicate or additional options are rejected.
  For JVM diagnostics this capacity MUST also be <= `spec.quota_bytes` because
  target-written JFR and JVM heapdump files use this shared filesystem. The standalone script
  uses 16 MiB for both the shared volume and helper `/out`, never a larger bound.
  Labels must include the same `owner_label=owner_value` supplied in the spec
  and `nanolab.diagnostic.tmp=true`. Pass its name as `target_tmp_volume`.
  Target and helper both use
  `--mount type=volume,source=<owned-name>,target=/tmp,volume-nocopy`.
  The provisioner inspects the existing volume before creating a helper, rejects
  missing/unowned volumes or arbitrary bind/plugin drivers, and verifies actual
  target/helper `Mounts` against that same writable volume and its mountpoint.
  Volume identity/options are checked again after helper start and retained in
  observations. It never issues a volume-create command. Main removes the volume
  only AFTER both owned containers are removed. Memory-only helpers do not use
  this volume and still have no mounts. The earlier `/proc/<hostpid>/root/tmp`
  Docker bind failed during OCI startup and is no longer used.
  JVM attach must remain enabled; do not set `-XX:+DisableAttachMechanism`.
- The helper's `/out` is its own size-limited tmpfs, mode 0700 and owned by the
  target UID. Node snapshot target file paths are translated to
  `/proc/<helper-namespace-pid>/root/out/<unique-operation>/...`.
  Same-UID proc-root access must be allowed by the local kernel/LSM. Failure is
  reported; the implementation never adds SYS_PTRACE or SYS_ADMIN to work around it.
- JVM heapdump stages in an exclusive `/tmp/nanolab-heapdump-<random>/` directory
  on the existing shared owned volume, rechecking identity and tmpfs capacity
  against both provisioned quota and request budget before `jcmd 1 GC.heap_dump`.
  No `-parallel` override, quota increase, or automatic retry is added. Completed
  command output, a nonempty bounded file, and an HPROF header are required before
  copying to `/out` for transport. The private staging directory is cleaned up.
  `heapdump-command.json` replaces the success-only `jcmd.txt`: it records actual
  argv, request/target, observed capacity/free space, and up to 4096 characters
  of command output with a truncation indicator. Supervised child failures retain
  the existing bounded exception/log excerpt, not a claim of complete stdout.
  The JSON is capped at 16 KiB and emitted before the result, including on command
  failure; its bytes count toward the same request artifact budget. Failure still
  triggers existing host cancellation, and incomplete HPROF files are not sent.
  Both shared `/tmp` and helper `/out` remain 16 MiB in standalone validation;
  the transport copy can temporarily coexist with target staging in separate
  bounded filesystems. This does not increase the target writer's quota.
  Changing staging does not establish that the observed segmented-file merge
  failure is resolved or caused by ENOSPC. Main must rebuild/publish helper r3
  and validate the actual dump; API and mount configuration are unchanged.
- JFR uses an owned temporary directory under shared `/tmp`, with the same
  canonical path in both containers. JDK25 `WriteablePath` creates a placeholder
  then calls `toRealPath`; proc-root magic paths are unsuitable for that writer
  and can leave a zero-byte file. The worker checks the actual shared tmpfs bound,
  requires nonempty JFR magic plus successful `jfr summary`, then copies the
  target-written bytes into `/out` for framed transport. Start/stop/summary
  outputs are retained in probe evidence; invalid files report the stop output.
  No full GC is invented or forced merely to make the provisioning probe pass.
  This worker change requires rebuilding/publishing the helper image. The
  standalone validation script now requires `--helper-image <new-repo@sha256>`
  rather than silently selecting the previous image.
- The owned host evidence directory is mounted read-only into the helper at its
  same absolute path. Device/inode observations establish this alias, satisfying
  the existing framed executor's namespace contract. Only the host executor
  writes artifacts there; target files never bypass `/out`.
- Node startup must preload this directory's `node-diagnostic-control.cjs`, e.g.
  mount it read-only at `/opt/nanolab/node-diagnostic-control.cjs` and append
  `--require=/opt/nanolab/node-diagnostic-control.cjs` to its existing Node options.
  `NANOLAB_DIAGNOSTIC_SOCKET=/tmp/nanolab-diagnostic.sock` is the default; an
  alternate `/tmp/<basename>` must also be passed as `spec.node_socket`.
  There is no inspector port, no port publication, and no `--inspect` flag.
  The mode-0600 Unix socket bridges to an in-process `inspector.Session`.
  Node must support `fs.statfsSync`, perf_hooks GC `detail`, and inspector sessions
  (use the source builder's pinned Node 22+ runtime).
- P24 target memory caps remain JVM 1 GiB and Node 512 MiB. Choose quota and
  target heap budgets to leave headroom for captures. Linux can charge tmpfs
  pages to the target that writes them; separate helper accounting does not
  remove that pressure. A 512-MiB Node heap snapshot under a 512-MiB target cap
  is not promised to succeed. OOM or ENOSPC remains failed evidence.

## Source image builder contract

`diagnostic-helper.Dockerfile` uses the `packages/nanolab` build context. Supply
digest-pinned `JDK_BASE` and `PYTHON_BASE` for linux/arm64. The JDK stage must expose
a glibc-compatible JDK25 at `/opt/java/openjdk`; Python must be 3.12+ at
`/usr/local/bin/python3`. The final image copies the existing `processes.py`
supervisor and new worker/JFC assets. Main must resolve/publish the resulting
RepoDigest, make it locally available, and supply it to prepare. A local tag or
image config hash alone does not satisfy the executor's immutable image contract.
The provisioner checks local RepoDigests, architecture, observed container image
ID and JDK versions. It does not fetch a registry or build an image.

## Evidence and cancellation

Preparation issues a receipt only after actual `jcmd VM.version` attachment and
target-written JFR probe output, or Node private-session attachment and a
target-written probe. Kernel mountinfo/statvfs establish the tmpfs bound; Node
also checks it from the target. Preparation records raw Docker/procfs/probe
observations beside the receipt. This is operator trust in pinned code, not
independent attestation.

JVM GC uses a fresh event-only JFR recording, `jcmd GC.run`, a target-side
recording dump and `jfr print --json`. Completion requires a request-window
`jdk.GarbageCollection` event with cause `Diagnostic Command` and a supported
full collector (`G1Full`, `SerialOld`, `ParallelOld`). Raw JFR and JSON are retained.
Other collectors fail explicitly. Counters count actual events in that fresh
recording; command exit is never full-GC evidence.

Node GC requires actual completed major-GC PerformanceObserver events in the
private collectGarbage request window. Snapshots use inspector chunk callbacks,
synchronous writes, the target-visible tmpfs bound and an additional byte limit.
No response is acknowledged while a snapshot callback is still writing.

Host Docker clients and remote JDK commands use the existing OwnedCommandRunner.
Timeout/protocol/remote-completion failures stop the exact owned target first,
then remove the owned helper. Killing the PID-namespace init also terminates
namespace members. Ownership/incarnation changes prevent signaling and produce
an explicit unconfirmed-cancellation error. Main must use `prepared.cancel()`
for external capture aborts; the provisioning cancellation Event alone does not
cancel an already-running executor exchange. Normal `.close()` removes only
the helper. Ownership intent is persisted before create for partial-start cleanup.

Live attachment, JFR semantics, Node events, quota behavior and aarch64 Docker
29.2.1 compatibility still require main's real validation and full P24 execution.
Synthetic unit tests are not evidence that the soak completed.

# Recipe preparation for the ARM64 release

## Intent and approved scope

Complete NanoLab's release build migration by replacing ARM64 Bake with recipes
v2. Reuse the AMD64 implementation, with explicit architecture and VM-role
selection, and make assembly and staging push separate phases on both
architectures. Preserve the complete image matrix, source identity, runtime
policies, benchmark/regression barriers, ARM64 smoke, public manifests and
signing contracts.

The user approved this design direction: three reusable ARM64 profiles,
architecture-aware shared execution, assembly -> staging push -> ARM64 smoke,
and the existing evidence/resume protections. This document is the written spec
for review; implementation starts after approval of this spec and a subsequent
implementation plan.

P24, other soak/load-test backends and smoke memory-limit review remain separate
roadmap work. This migration does not authorize a public release, publication or
signing during verification.

## Baseline and current boundary

NanoLab main is `46894690246a1e059913151b97e2b649855e733b`. Design against the
existing clean NanoFaaS CI pin
`e7914be065e844776af57fe9e449bce7f12e03c5` (version `0.22.0`). Changing the pin is
not part of this slice.

AMD64 uses three frozen recipe profiles, verified source inventories, owned
builder selection and independent image inspection. Its build receipt records
local image IDs; its separate push receipt records staging registry manifest
digests. The full suite passed with 3,265 tests after the final review fixes.
Real native-AMD64 build/export/runtime verification remains incomplete because
Azure provisioning required MFA. Merging that implementation did not qualify
those unexecuted gates.

ARM64 currently stages Bake inputs, prepares JVM artifacts, loads images on the
ARM VM and pushes them inside `_build_arm64_images`. The `arm64-build` receipt
therefore contains registry digests. Smoke and publication read those digests
from this combined phase. All local-image resume verifiers currently target the
stack VM; ARM64 recipe assembly requires verification on a different daemon.

## Matrix and profiles

Add reusable profiles in `packages/nanolab/recipes/`:

| Profile | Images at the pin | Tag suffix | Selection |
| --- | ---: | --- | --- |
| `release-arm64-jvm.yaml` | 9 | `-arm64-jvm` | Control plane, seven Java functions, warm-echo |
| `release-arm64-native.yaml` | 12 | `-arm64-native` | Same nine plus three Java-lite functions |
| `release-arm64-default.yaml` | 23 | `-arm64` | Six Bash, five Go, five JavaScript, six Python functions and watchdog |

These 44 cells are the ARM64 partition of the source-derived release matrix.
Counts describe the current pin; exact coverage is checked against the guarded
archive, not maintained as a second catalog. Missing, extra or duplicate cells,
modules or identities fail preflight before any provider acquisition.

Use schema 2, repository `127.0.0.1:5000/nanofaas`, explicit existing image names
and version overrides through `-PrecipeTag`. Omit `registry.platforms` and
`registry.provenance`: each architecture builds natively into its own Docker
daemon. Public multiarch indexes remain a later publication responsibility.

Preserve the resolved eight-module selection used by the AMD64 profiles:
async-queue, autoscaler, build-metadata, concurrency-control,
k8s-deployment-provider, offload, runtime-config and sync-queue. Match the guarded
source's resolved `all` behavior; do not silently add the two conflicting
providers from the ten-module catalog.

Preserve JVM G1/C2 and control-plane variant `jvm-g1-c2`. Spring native components
use Oracle GraalVM, optimization 3, G1 and effective JFR, with control-plane
variant `native-o3-g1`. Java-lite uses Community GraalVM, optimization 3 and
serial GC. Preserve source-owned runtime/AOT defaults, ports and permissions.
Actual ARM64 support for these native policies is a mandatory capability gate,
not inferred from successful AMD64 schema validation.

The default profile declares the schema-required JVM control-plane artifact
without a container. Its exact artifact-only component has `image: null` and
is validated separately; it does not add a 45th image or broaden other recipe
consumers' handling of missing images.

Freeze both architecture plans and all six raw profile byte strings, hashes,
resolved tags, modules and component mappings before provisioning. Updating a
profile after preflight cannot alter that request's execution inputs.

## Shared execution and resource ownership

Extend the existing release-specific recipe preparation, command generation,
staging and execution helpers to accept validated architecture and execution
role. Limit this to the two supported pairs: AMD64/`stack` and ARM64/`arm-builder`.
Reject mixed architecture groups or a mismatched role. Retain one implementation
of report validation, inventory checks, transfer verification and image proof.
Do not create another recipe runner, a task framework or an architecture-to-host
heuristic. No Sonata or NanoFaaS plugin change is assumed by this design.

Use distinct local and remote paths per architecture:

- transient profiles/configuration: `recipe-inputs/<architecture>/`;
- remote producer outputs: `recipe-output/<architecture>/<flavor>/`;
- remote logs: `recipe-output/<architecture>/logs/<flavor>.log`;
- retained evidence: `<release-dir>/recipe-evidence/<architecture>/`.

Configuration names and builder names also include the architecture. Logs must
remain outside the producer's output directory: shell redirection inside it
violates `cleanRecipe` ownership and can be deleted during assembly. Clear stale
group reports and logs before each attempt; require successful assembly before
accepting any image or report. Never use outputs from a failed attempt.

Each VM uses an owned `docker-container` builder with `default-load=true`, the
existing BuildKit `max_parallelism` ceiling, `DOCKER_BUILDKIT=1` and explicit
`BUILDX_BUILDER`. Preserve the preexisting selected builder on the same VM during
successful and failed acquisition. Native compiler memory/parallelism remain
separate from BuildKit concurrency. Record actual Docker/Buildx/BuildKit facts,
builder identity and configuration hashes. Check native Linux host and daemon
architecture, normalize equivalent architecture spellings, and independently
check Linux/ARM64 image configuration IDs for ARM64. Reported builder platform
support alone is insufficient; emulation does not qualify this slice.

Retain the owned registry tunnel from the ARM VM's loopback port 5000 to the
stack registry. Assembly loads local ARM64 images and does not push. The push
phase uses that tunnel; registry evidence describes the stack registry. Preserve
the existing VM ownership, credential lifecycle and teardown contracts. Partial
acquisition, cancellation and cleanup failures compensate only owned resources
and preserve the original failure and any available diagnostics.

## Guarded source and evidence

Use the same frozen archive, original commit and inventory for both VMs. Verify
the staged archive hash and original source paths on each VM before and after
recipe work. Check all original entries, including committed files below build
outputs, and reject unexpected additions except source-derived permitted Gradle
outputs/caches. Preserve file modes and symlink protections. Do not synthesize
Git metadata or copy a dirty checkout over the archive.

Source tests keep their existing selections. Their Python editable packaging,
virtual environment and pytest cache remain in the owned external test workspace,
including the original `pytest.ini`; they cannot contaminate the recipe context.
An archive report's `source: null` is accepted only with the separate verified
archive/inventory binding. A non-null source must match the guarded commit.

Reuse the strict release distribution adapter: raw profile hash/name/tag,
schema, modules, exact component kind/SDK/name/mode, options and expected image
references must all agree. Independently inspect all local image IDs/platforms,
then recheck the complete union after the third assembly. An image replacement
by a later group must not leave a complete receipt for earlier stale IDs.

Fetch complete raw reports, full bounded logs and build facts with remote
size/hash checks before and after transfer and local digest equality. Retain
profiles, configuration, archive/inventory binding and report/log evidence before
writing a complete receipt. Unavailable, changed or truncated evidence fails
closed. Keep transient input reacquisition separate from retained receipt files;
resume validation must not repair or overwrite missing evidence before checking
it. Hashes of commands, environment, working directories and retained evidence
are part of the execution contract on both architectures.

## Release DAG and receipt transition

The release order becomes:

```text
source tests
  -> AMD64 assembly -> AMD64 staging push
  -> three benchmarks -> aggregate -> regression gate
  -> ARM64 assembly -> ARM64 staging push -> ARM64 smoke
  -> architecture publication -> multiarch manifests -> aliases
  -> signing/attestation -> finalization
```

Shared source staging can still acquire ARM64 infrastructure earlier; this
ordering describes phase work, not a claim that VM acquisition is sequential.

Keep phase `arm64-build` and title `Build ARM64 images` for assembly only. Its
receipt now covers the exact 44 `local-image-digest` entries with
`docker-daemon:` references, plus hashed input/report/log evidence. Introduce
phase `arm64-local-registry-push`, title `Push ARM64 images to local registry`,
with one `local-registry-digest` entry per expected `docker://` reference.
Use the existing phase task and registry-push primitives for this additional
release phase; do not add a generic publish-only recipe task.

The ARM64 assembly depends on genuine source-test and regression receipts. Its
push depends on the verified assembly receipt; it independently rechecks the
local image IDs against that receipt before pushing. Preserve registry digest
inspection and exact matrix coverage. Never substitute a Docker config ID for a
registry manifest digest. ARM64 smoke consumes the new push receipt and compares
its runtime image/digest mapping with that exact receipt. Publication barriers
consume both the passing smoke record and the new registry receipt, together
with unchanged AMD64 and regression evidence.

Update receipt readers explicitly; no reader may interpret `arm64-build` as a
registry receipt after this migration. Old combined Bake receipts are not
converted or trusted as recipe evidence. Bind the new contract to the assembly
fingerprint and required evidence so old journals cannot skip assembly, the new
push or smoke. Preserve unrelated journal history. Existing CLI selection
remains: `--until build-arm64-images` now stops before staging push; the new
`--until push-arm64-images-to-local-registry` stops after it. Document this
intentional selection boundary change.

Remove ARM64 Bake staging and direct JVM/native preparation commands from the
release DAG. Delete their helpers only after checking for remaining callers;
retain Bake functionality used by other workflows. Public references, index
platforms, aliases, signing subjects and benchmark thresholds remain unchanged.

## Resume and architecture routing

Keep existing evidence kinds and reference formats. Bind each expected
`docker-daemon:` reference to its VM using the frozen release matrix: AMD64
references inspect `stack`, ARM64 references inspect `arm-builder`. Unknown or
ambiguous references fail closed. Do not guess a host by substring matching an
unvalidated tag, use the client-selected Docker context or probe the other VM as
a fallback. Staging registry digests are inspected on `stack`, independently of
local image routing. GHCR and signing verifier contracts stay unchanged.

Fingerprint each architecture's assembly with profiles, tags, complete cells,
mode/options, archive/inventory hashes, commands/environment/cwd, role, builder
identity/driver options/configuration and concurrency policy. Fingerprint push
with exact image mappings and prerequisite receipt hashes. Bind smoke and
publication to the new push receipt and genuine upstream gate receipts.

An unchanged resume performs verification, with no Gradle builds or image
pushes. Deleted/tampered reports, profiles, configuration, inventories, receipts
or replaced local tags invalidate the affected phase and its consumers. A
change on one daemon must not be judged on the other daemon or incorrectly
invalidate an independent architecture. AMD64 behavior remains protected by
regression tests. A failed rerun invalidates its old complete receipt; no partial
group or partial push set authorizes smoke, public publication or signing.

## Verification and completion gates

The implementation plan must cover these gates explicitly:

1. Validate all six profiles with pinned Gradle and exact archive-derived
   coverage. Reject profile/catalog/module drift before provisioning.
2. Prove on a native ARM64 release-compatible VM that the installed toolchain
   selects the owned builder, loads a tagged image locally, and exports real
   compiled Oracle G1 and Java-lite binaries through recipes. Inspect effective
   commands/options and exported artifacts. If unavailable, record an incomplete
   gate; if unsupported, revise this spec rather than changing policy or using
   Bake/emulation as a fallback.
3. Test shared execution for both architecture/role pairs, wrong-VM and wrong
   image architecture rejection, artifact-only components, stale/partial reports,
   source changes, log/output ownership, truncation, cancellation and compensation.
4. Test resume routing with the same logical target on both daemons, an ARM tag
   absent from stack but present on ARM, replaced local tags, missing retained
   files, changed source/profile/options and legacy combined receipts. Prove
   unchanged resume performs no assembly/push; downstream invalidation is exact.
5. Test DAG and CLI selection boundaries, genuine source/regression prerequisites,
   the separate ARM push receipt, smoke digest binding and later publication
   barriers. Full suite, quality, formatting and type/import checks must pass;
   report the four baseline Bandit B101 lows separately, without suppressions.
6. Run the real ARM64 assembly/staging-push/runtime slice on a native VM with the
   guarded archive. Retain all 44 local IDs/platforms, three raw reports, actual
   builder/native export facts, full logs, all registry manifest digests and
   runtime smoke results. Exercise real unchanged resume and owned teardown.
   Preserve the registry's AMD64 partition and unrelated resources.
7. Exercise the executable release DAG through ARM64 smoke with actual source
   tests, AMD64 phases and benchmarks/regression receipts; do not manufacture
   passing prerequisites or relax thresholds to reach ARM64. An isolated ARM
   capability/slice run validates its scope, not the canonical release DAG.

Azure MFA can block both native VM gates. Local implementation and controlled
regressions may proceed after pinned profile/catalog checks, as with AMD64, but
cannot replace native builder/export/runtime evidence. A local native-ARM host
probe, if used for diagnostics, does not qualify the release VM or canonical DAG.
The independent AMD64 verification gap remains open until actually executed.
A failed existing performance gate is reported; its policy is a separate issue.

Migration completion requires the shared runner, exact receipt transition,
architecture-aware resume and real native ARM64 gates above. Public release
qualification additionally requires both architectures' outstanding verification
and all existing publication/signing gates. No public push or signing belongs
to these migration verification runs.

Update the README, profile documentation and roadmap with supported paths,
selection changes, retained evidence and any incomplete gates. Leave the roadmap
item unchecked until its required evidence passes.

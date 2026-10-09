# Rust function integration — issue #57

The bounded implementation reuses discovery, root-context Dockerfile builds,
registration and the family artifact-contract workflow. It extends the existing
`release.source.rust` command, rather than adding another task. No dependency,
new workflow or Rust soak prerequisite was added.

## Sources and distribution

- NanoLab base: `81b82609d8b66281c7bcf1157076d16139229b8d`, after PR #78.
- NanoFaaS source-contract CI pin: `6570aaf0c2287201013bd09406c2d7905c2666c5`.
  This is the first revision with all four Rust functions **and** Rust recipe
  schema, Dockerfile assembly and k6 support. The preceding `90c60903` has the
  functions but rejects `sdk: rust` during real Gradle recipe assembly.
- Direct artifact and source-test qualification also used clean NanoFaaS main
  `b1aa7f65c7ed0a70c2b94f37092fe62b7e877934`.
- Work stays on `fix/operational-validation`. NanoFaaS operator sources have no
  tracked diff; SHA-256 checks preserve all 19 preexisting untracked files.
- Installed dependencies: 51 packages, compatible; public Sonata pins unchanged.

The default release profiles now contain all four Rust images. The six profiles
resolve to 9 JVM, 12 native and 27 default images per architecture: 48 per
architecture, 96 total. All six passed real upstream `validateRecipe` on the
supported source pin, including both Rust-bearing default profiles.

## Test-first changes and source tests

Initial behavioral regressions failed before implementation: Rust discovery,
root-context builds, image/family matrices and the SDK/function source loop.
Separate red/green checks caught unsupported recipe SDK mapping, the obsolete
Rust exclusion in qualification markers, missing async preset entries and the
watchdog cache-path interference. Pure Rust tests pass with `NANOFAAS_ROOT` unset
(9 tests); the broader targeted run passed 90 tests.

The generated source-test shell runs `cargo test` for `sdks/rust`, word-stats,
json-transform, roman-numeral and qr-code, stopping at the first failed suite.
It shares `build/cargo-target` for those crates, then unsets `CARGO_TARGET_DIR`
before watchdog tests, whose local smoke script requires its own target path.
Real execution on main passed 103 SDK tests, 16 reference-function tests and
25 watchdog Rust tests, followed by all 82 watchdog local smoke tests.

The pinned compiler remains `rust:1.97.1-alpine3.21`. Its digest is now the OCI
index `sha256:7bae7c67364dad5ebbd4060923b34d734fbed66d7c1cf3af72aa2f062af93eb6`,
which contains the previous exact AMD64 manifest
`sha256:e5c73e7a712b368eb90b1190c6e1c4a01a3ebb0fe0abfff68c3bcd2df26ecc41`
and the ARM64 manifest
`sha256:33ecfb3c72d1ad8370b1a2fd797f6f02a1a71e2393053ac02d2bbc31503bf38d`.
The old single-architecture digest failed on the ARM64 host with `exec format
error`; the index executed natively. Python and musl headers support the SDK
wire-contract validation and static Rust builds.

## Installed artifact contracts

On native Linux ARM64, the installed wheel built and exercised all four Rust
images on main: **18 HTTP cases and 18 actual callbacks**, including error status
and QR PNG checks. No watchdog one-shot case belongs to this Rust-only subset.
The marker records the exact four-cell subset and verified cleanup. An
independent pass checked every receipt hash and absence of all 11 owned
containers, images, network and builder resources.

Qualification SHA-256:
`349d2a237dddff465613ad1e7408f0dbc2b52ff97dd9e77a41c6f52b24320005`.
Artifact wheel SHA-256:
`31e2e395dc56fa84e28c9f03c46da6b985786f53bc883b9d250d779c05c17e6e`.
The later k6 candidate wheel changes release source-test execution and the four
broader validation presets; contract execution code is unchanged apart from
formatter normalization of one equivalent string literal. The wheel comparison is
retained in `installed-wheel-comparison.json`. The k6 candidate SHA-256 is
`687443e2c74788c9d058d23c69bf0d4fe945cdd4fd3acd0f101943cca576cb81`;
all 222 packaged Python modules have the same AST as the final working source
(`installed-candidate-source-comparison.json`).

The supported catalog has 31 full-preset cells and 155 cases (137 HTTP and 18
Bash one-shot). This Rust subset does **not** requalify the entire 31-cell preset;
the older 27-cell qualification remains historical evidence on its own source.

## Container k6 qualification

The installed verification completed successfully. The harness uses installed NanoLab
`recipe_distribution_resource`, `recipe_compose_resource` and `add_platform`,
the shared build/deployment/registration core, against main. It assembles a
recipe with the standard loadtest modules (container provider, autoscaler,
async queue and build metadata), verifies metadata
and the control-plane image, invokes three Rust functions, then verifies each
running container by function label and exact distribution image ID before
running the three Rust rows from upstream `run-all.sh`.
The final verification consumer holds the platform and function resources until
all rows finish, followed by the normal workflow releases.

The copied upstream harness filters only `RUNTIMES` to Rust. Benchmark code,
small payload corpora, 110-second stages, 10-second cooldown and thresholds remain
unchanged: p95 < 3000 ms, p99 < 5000 ms, HTTP failure rate < 15%, response validity
100%. The unfiltered benchmark and run-all files are identical between the
source-contract CI pin and main. The first two-module validation setup failed the load thresholds; the final
recipe uses the existing loadtest module set, without relaxing thresholds or
changing the benchmark.

All three rows passed with zero HTTP failures and 100% valid responses:

| Row (`small`) | Requests | p95 (ms) | p99 (ms) |
| --- | ---: | ---: | ---: |
| word-stats-rust | 13,255 | 5.48 | 9.05 |
| json-transform-rust | 13,287 | 4.87 | 7.38 |
| roman-numeral-rust | 13,326 | 4.42 | 6.38 |

Total: **39,868 requests**, all original thresholds passed. All 18 workflow
operations, including resource releases, passed. An independent check verified
absence of all three functions' containers, both owned Compose projects and
networks, and the temporary registry. Eight owned host image tags and one
untagged owned image were removed; operator Minikube and the heap-analysis
builder remain stopped and preserved. Raw summaries, logs, image identities,
workflow outcomes and independent cleanup records are retained.

## Checks and limits

The first full suite on the function-only pin passed 3,891 tests with 3 skips.
Branch coverage was 86.36% and the original 90% gate failed. The final suite on
`6570aaf0` also passed 3,891 tests with 3 skips; branch coverage was 86.52%, below
the unchanged 90% gate. No threshold was lowered; coverage debt existed before this
change. All 15 pre-commit hooks passed, including typing, imports and security.

One independent read-only reviewer found no remaining implementation defect
after correction of the source pin. The same reviewer checked the shared
platform route and its scope, independently recomputed all three k6 summary/log
hashes and request totals, and found no remaining Critical, Important or Minor
issue. The deferred boundaries below were accepted for this bounded integration.

A separate main-source container workflow succeeded at invocation but failed
legacy container inspection: main now appends a hash to managed container names
for every runtime. Generic container identity migration remains separate from
Rust integration. Main direct-artifact and source-test results remain valid;
the generic `validate` inspection on main was not qualified by that separate k6
platform harness. A subsequent label-based inspection fix independently qualified
the standard workflow; see [container inspection evidence](2026-10-09-managed-container-inspection-evidence.md).
No product check was removed or weakened.

The source-contract CI pin also exposed an older upstream SDK defect:
optional invocation metadata/headers serialized as JSON null cause a malformed
request response. The nullable-map fix (`e0c874b9`) is already in main, which is
therefore used for live Rust platform qualification. CI discovery, image/matrix
and recipe checks on the earlier pin do not qualify that older SDK for live
control-plane workloads. Raw failed attempts retain both incompatibilities.

Rust soak gauges, full containerd/Kubernetes operational coverage, cross-
architecture execution, QR decoding and newer optional-module compatibility
remain outside this bounded change. Existing Go/JavaScript source-loop handling
and other toolchains' architecture support were not changed.

Raw local evidence is retained under `/tmp/nanolab-rust57-evidence`, including
source state, installed wheels and hashes, red/green logs, real source-test argv
and result, six Gradle validation results, artifact receipts and cleanup audit,
k6 filter and workflow, and the preserved failed main/initial-pin attempts.

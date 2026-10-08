# Packaged function and watchdog contracts — issue #54

Status: proposed third delivery; awaiting written-spec review. Work stays on
`fix/operational-validation`. The previously approved subdivision authorizes
designing this delivery; it does not approve its implementation yet.

## Purpose and observed inputs

An operator must know that the packaged function, including its native runtime,
returns the independently specified body/status and completes the actual callback
protocol. Handler unit tests and a successful image build cannot provide this
qualification. Run the packaged watchdog in warm and one-shot modes too.

The current NanoFaaS source was inspected at `a234ea17`, based on `a3722a47`.
NanoLab's catalog discovers 23 implementations of `word-stats`, `json-transform`,
`roman-numeral` and `qr-code`. Its existing image plan expands these into 27
host-architecture image cells: Java JVM/native, three Java-lite native functions,
and Python, JavaScript, Go and bash images. There is no Java-lite QR implementation.
These counts are observations, not hard-coded acceptance thresholds.

The shared corpus has 3/3/5/7 cases respectively. All packaged payloads in these
families map uniquely to that corpus after reading their `input`; QR artifacts
have no packaged payload files and must exercise the seven corpus cases directly.
The embedded `expected` in a payload file is not the independent oracle.

The standalone watchdog image is scratch and contains no function command. The
bash function images contain both the packaged watchdog and the real handler.
They are therefore the existing artifacts to qualify in warm and one-shot modes.
Warm watchdog responses deliberately have no callback. SDK callbacks may omit
`statusCode` for plain results; the effective status is then 200.

## Approach

Add a local Linux/Docker `contract` workflow. Reuse the existing catalog,
`build_image_plan`, Bake renderer, frozen-source acquisition, command execution
and Sonata resource/compensation primitives. Keep contract semantics in NanoLab
and keep the current public dependencies unchanged.

Alternatives considered: extending `validate` would introduce a control plane
whose mediation obscures direct callback behavior; invoking the in-process
contract harness would still qualify handlers instead of packaged runtimes.
The explicit workflow provides direct artifact evidence and normal resource
ownership without creating another build engine.

## Selection and build

- Bundle `artifact-contract-container.yaml` for the full supported four-family
  matrix and a small `artifact-contract-smoke-container.yaml` for `word-stats`.
  Both select `workflow: contract`, `backend: container`, local environment.
- Derive the default matrix from supported catalog entries with independent
  corpus files. Explicit function selection may narrow it; every selected
  runtime flavor and every corpus case must run. Reject unknown selectors,
  unsupported runtimes, empty selections, missing/ambiguous expectations and
  malformed inputs before acquiring platform resources.
- The first delivery supports the native host architecture only. Reject remote
  providers, Kubernetes/containerd, external endpoints, image publication,
  `--keep`, teardown, resume and partial task selections for qualification.
  No emulation or privileged binfmt setup is needed.
- Freeze one source snapshot for discovery, payloads, corpus and all builds.
  Reuse the selected image plan's JVM prerequisites and Dockerfile/native build
  arguments with `--load`, a private Buildx builder and run-specific tags.
  Never change the operator's selected builder or build in its source checkout.
- Capture source/corpus hashes, build inputs, image ID, architecture, entrypoint
  and started container identity. Native Java cells additionally retain and
  validate the executable's ELF architecture and hash; a JVM executable cannot
  qualify a cell labeled native. Reuse the existing native identity checks
  where applicable. Recheck image/container identity before accepting results.

## Owned execution and callback capture

Use one private Docker network, a small bundled stdlib Python capture/probe
container, and one function container at a time. Run the artifact's existing
entrypoint, set its callback URL to the owned capture service, and send requests
from that private network. No host listener or published application port is
required. Readiness checks must not invoke a function or manufacture a callback.

The capture service acknowledges valid callback requests immediately with 2xx,
records their method/path/body, and provides bounded inspection to the probe.
The execution ID comes from the `/{executionId}:complete` URL, not an invented
required field in the callback body. IDs are unique across the entire attempt.
Unknown IDs, duplicate deliveries, malformed JSON/duplicate keys and unexpected
routes fail the attempt and remain diagnostic evidence. A startup self-check
proves capture and inspection using a separate probe ID, never an artifact ID.

Set explicit budgets: 60 seconds for readiness, 30 seconds per HTTP request,
15 seconds for callback arrival, and a 2-second observation period after runtime
shutdown before the final callback audit. Bound each request/response/callback
to 2 MiB, logs to 8 MiB per container and capture to 1,000 records / 32 MiB total.
Reaching a bound fails qualification; it must not truncate evidence into success.
Use a 45-minute build deadline per image. The private builder has a 16 GiB memory
limit and four CPU equivalents; record and verify those Docker resource limits.
Also use native memory/parallelism flags where the existing build accepts them;
do not pretend that an unused Docker build argument limits Java-lite builds.
These bounds are configurable only through
validated contract settings; defaults remain usable by the bundled scenarios.

## Observable contracts

Run all independent corpus cases. Wrapped packaged payloads are checked for
unique correspondence and consistency with the oracle; they do not replace
corpus cases or generate expected results from the handler under test.

For ordinary SDK artifacts, send `POST /invoke` with a unique `X-Execution-Id`
and `{"input": ...}`. Require the exact expected HTTP status and semantic JSON
body, plus exactly one successful callback carrying the same output and effective
status. An absent callback status means 200; an absent status cannot qualify a
400/422 response. Callback transport failures or runtime error envelopes fail.
Array order and application fields remain meaningful. Expected application
errors, including the corpus's word-stats error with status 200, remain valid;
an `error` property alone is not an infrastructure failure.

For successful QR cases the oracle specifies PNG output rather than exact bytes.
Require the shipped envelope/content-type/encoding contract, strict base64 where
used, a structurally valid PNG and the requested/default dimensions. Compare the
decoded callback PNG bytes with that invocation's decoded HTTP output when the
transport representations differ. Cross-SDK PNG byte equality
and independently decoding the QR text are not claimed. QR error cases still
require their exact corpus JSON body and HTTP status.

For bash artifacts in warm mode, validate the same HTTP body/status contracts and
require zero callbacks, even with the capture URL supplied. For one-shot mode,
start a fresh container per case with `WARM=false`, `EXECUTION_ID`, `CALLBACK_URL`
and `INVOCATION_PAYLOAD` containing the same wrapped request JSON. Preserve its
real watchdog command/handler. Require
exit 0 and exactly one complete callback with the corpus output/effective status.
There is no HTTP response to assert in one-shot mode. No synthetic handler is
substituted into the standalone scratch image.

Always reject missing reflection/resource registrations, unsupported native
features, serialization failures, Python tracebacks and Rust panics in artifact
or capture/probe logs. The expected business-error JSON is not such a diagnostic.

## Evidence, cleanup and qualification

Write immutable receipts for every image, mode and case, including raw bounded
response/callback bytes, parsed observations, logs and failures. A restricted
selection reports its exact qualified subset. The default matrix currently
produces 119 HTTP case executions plus 18 bash one-shot executions; derive these
counts from frozen inputs instead of assuming the issue's earlier artifact count.

Containers, capture, private network, run-created images and private builder are
Sonata resources with compensation for partial acquisition. Capture stays alive
through runtime shutdown and the final duplicate audit. Verify ownership before
destruction, remove only this attempt's resources, and record independent absence
checks. Any failed build, contract, diagnostic, observation or release leaves the
workflow failed. Write the final qualification marker only after all required
cases and cleanup have passed; planning/build-only slices cannot create one.

Rust functions remain #57. Other families without the shared corpus, fixture
functions, service artifacts, full watchdog HTTP/FILE supervision coverage and
cross-architecture qualification are explicit exclusions. CLI shipped-source
qualification remains a separate gate; this delivery alone does not close #54.

## Verification and handoff

Use RED-to-GREEN tests for selector/oracle validation, actual HTTP/capture exchange,
wrong status/body, missing/duplicate/late callbacks, wrong IDs, omitted status,
PNG validation, native identity, diagnostic rejection and ownership/cleanup
failure. Test transports may replace Docker commands but cannot manufacture the
live qualification callback. Test selection must preserve the CI distinction
between pure tests and explicit NanoFaaS checkout contracts.

Before claiming completion, build/install the wheel and resolve both presets;
exercise real JVM/native/Java-lite and other selected SDK artifacts plus real
watchdog one-shot cases on an owned local Docker target; inspect cleanup; run the
full default matrix for full qualification. Run both original package suites,
coverage configurations, dependency checks and quality hooks. Partial live runs
remain explicitly partial, even when every unit test passes.

After written-spec approval, create the operational plan with `writing-plans`
and obtain its execution-method decision before implementation. Keep the same
branch and update its existing PR when a reviewed, verified delivery is ready.

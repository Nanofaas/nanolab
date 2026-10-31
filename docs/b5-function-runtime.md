# B5: equivalent word-statistics functions

B5 runs Java, Go, JavaScript, Python and Rust **separately**, with one function
replica and one common control-plane policy. Use the five bundled
`function-runtime-{java,go,javascript,python,rust}.yaml` scenarios with the existing
`loadtest` workflow. These are runtime comparisons, not autoscaling experiments.
The older concurrency-cycle presets retain their governor-testing purpose.

All five handlers implement NanoFaaS's `docs/word-stats-contract.md`. Its generated
`runtime-contract.json` supplies adversarial correctness cases and expected
outputs for all four unchanged `performance-medium.json` inputs. The generator
refuses absent/stale expected inputs; setup invokes all four deployed payloads
and aborts before measured traffic if any output differs. Every measured 200
response is also checked against the exact expected output. SDKs and transport
are retained: the experiment compares the complete language/runtime stack.

## Fixed deployment and workload

- Control plane: source JVM image, full tiering, Serial GC, 2 CPU / 2048 MiB limit
  and 0.25 CPU / 512 MiB request. Freeze its source, module set, configuration and
  deployed image identity across all arms before interpreting a comparison.
- Function: one replica, 2 CPU / 512 MiB limit; 1 CPU / 256 MiB request.
- Shared async-queue scheduling strategy, including synchronous invocations:
  concurrency 2, queue 20, timeout 30000 ms, maximum retries 3. No sync-queue,
  concurrency governor, autoscaler or client retries. No per-request handler logs.
- Medium corpus; deterministic four-case rotation using the global scenario
  iteration index (independent of VU reuse). One open-loop scenario, the
  same 450-second shape for every language: 30s/40, 60s/200, 60s/200, 10s/600,
  30s/600, 10s/200, 60s/200, 60s/350, 45s/350, 10s/900, 30s/900, 45s/40
  (duration / target requests per second). Start rate 40/s; 5000 preallocated and
  maximum VUs; 30s HTTP timeout and 40s graceful stop. No CLI stages override it.

Scheduled arrivals, actual offered requests and k6 `dropped_iterations` are
separate quantities. Five arms need 37.5 minutes of scheduled traffic, plus
qualification, drain, provisioning and builds. This first run is a pilot of the
load shape, not a maximum sustainable-throughput claim or a statistical ranking.
Check generator saturation, control-plane saturation and throttling before
attributing a difference to a function runtime. Repeat and vary the common load
only after this calibration; never tune the load separately per language.

## Evidence and accounting

`k6-summary.json` retains `b5_offered`, `b5_valid_completed`, `b5_refused`
(429/503), `b5_invalid_output` (200 with an incorrect body), and `b5_http_error`
(other HTTP/transport failures). All five counters are initialized, including
zero outcomes. `b5_valid_duration` contains only valid completions; no successful
samples means latency is unavailable. Ordinary HTTP duration mixes refusals and
successes and must not be presented as successful-execution latency.

The existing console ledger records every response, its phase (qualification or
measurement), classification, status, transport error, duration and timestamp. Each record also
includes `case_name` and `iteration`, the global scenario iteration index;
qualification requests have a null iteration. This permits reconstruction of
the actual input mix, including accepted and refused requests.
It stores no response bodies. `http-status-counts.json` reconciles all HTTP counts
with k6, and separately reconciles the measured outcome counts with B5 counters.
Qualification HTTP requests remain visible but do not count as measured offered
load or useful throughput. A truncated ledger fails accounting, preserving the
summary and ledger for diagnosis. Overload and incorrect outputs remain findings
in the retained evidence rather than aborting later arms.

The workflow enables cAdvisor and retains function and control-plane working set,
RSS, CPU rates, raw cumulative CPU seconds, and CFS quota/throttling counters.
Working set and RSS have distinct names. Preserve raw labels to detect restarts
and distinguish replacement pods when calculating counter deltas. Existing queue,
retry, timeout and control-plane JVM telemetry remains available. No JVM heap
metric is used as a substitute for another runtime's resident memory.

## Execution boundary

This change prepares and tests the workflows; it does not launch B5. At launch,
use workflow-owned Azure Spot provisioning, isolated controller/source/results
roots under `/data`, frozen revisions, a committed resolved campaign manifest,
and existing cleanup hooks. Leave build CPU and available RAM unrestricted, as
in the current campaign. Build budgets are distinct from runtime resource limits.
Archive raw results before Python analysis and generate tables/figures by script.
Do not change the sources or image pins of running A/B experiments.

Unicode category tables differ across language releases; the common fixtures
cover established characters. The measured ASCII corpus avoids that boundary.
Rewriting handlers changes semantics for punctuation and edge cases, so old
measurements must retain their original revisions and must not be pooled with B5.

## Legacy input-rotation audit

The frozen A1/A1b/A2 `runtime-comparison.js` and A3 `mixed-workload.js` select
text by VU-local `__ITER`; the B1 open-loop generator and co-tenancy/soak
generators use the same pattern. Input availability is identical, but different
VU reuse under different latency can change the actual corpus proportions.
Existing aggregate archives do not establish the exact per-input distribution.
This is an additional comparison limitation, not evidence of a quantified bias
or grounds to pool old and corrected runs. B5 fixes it with
`execution.scenario.iterationInTest`; historical scripts and running campaigns
are preserved. Assess the impact before fine-grained performance claims or
deciding which experiments require a rerun.

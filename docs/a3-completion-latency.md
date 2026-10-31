# A3 admission-to-terminal latency

Set `completionLatency: true` on the four existing mixed-profile A3 scenarios.
Keep the two word-statistics functions, JVM C2, two control-plane CPUs, 2048 MiB,
5000 VUs per function, scales 2/3, and async/idempotent shares 0/0 or 0.20/0.05.
Run the four arms again on one isolated Spot pair through `nanolab compare`.
Use explicit `/data` run directories and a final provider teardown hook, as in
the first A3 campaign. Preserve the earlier campaign as separate evidence.

The option requires a NanoFaaS build exposing
`function_completion_latency_ms_seconds_{bucket,count,sum}`. The advanced
metrics profile publishes the histogram. Preflight checks the management port
before k6 starts; the snapshot requires all three families for both functions.
Queries preserve function, path (`sync`/`async`), outcome
(`success`/`error`/`timeout`) and bucket bounds rather than mixing populations.

NanoFaaS records the existing terminal observer's original admission-to-terminal
duration once per execution, including queue wait and retries. The engine's
existing duration is resolved in milliseconds. Replayed keyed requests are not
new executions. Refused requests are excluded because they were not admitted.
The aggregate e2e timer read by the concurrency controller is unchanged.
The new timer is diagnostic and is omitted by the basic metrics profile.

This measures server-side conclusion time, not client network delay or the time
at which a client observes the result. Keep async ACK latency separate. Save raw
cumulative buckets, counts and sums for offline window deltas, means and
histogram percentile estimates; retain reset/publisher labels and mark incomplete
coverage. Percentiles reconstructed from buckets are estimates. Preserve the
trailing collection margin and distinguish work admitted in the load window
from work that happens to conclude in it. No polling traffic is added.

## HTTP response accounting

The first completion-repeat attempt was interrupted at the user's request and
released its Spot pair. Restart all four arms in a new directory after fixing
the mixed generator. Each keyed request now receives its own answered-status
check; the execution-ID agreement check remains conditional on two HTTP 200s.
The arrival schedule, payload, headers, request timeouts and replay policy stay
unchanged.

Every mixed-profile HTTP response, including probes and both keyed requests,
is written to NanoLab's existing k6 console-event ledger (`k6-events.jsonl`).
The event carries function, path, keyed/non-keyed, first/second/single, status,
k6 transport error code, VU/iteration, timestamp and duration. No response bodies
or headers are logged. The existing fetch-on-failure mechanism retrieves it.
After collecting the normal run artifacts, the workflow writes
`http-status-counts.json` and verifies that total responses and HTTP failures
reconcile with k6. A damaged ledger fails verification after retaining evidence;
unexpected HTTP statuses remain findings and do not abort later campaign arms.
The extra diagnostics incur load-generator file I/O, with no extra HTTP traffic.

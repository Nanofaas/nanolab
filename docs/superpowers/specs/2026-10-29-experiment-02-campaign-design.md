# Experiment 02 campaign preparation

Author approval: start plan 4 on 29 October 2026, with minimal machinery; subsequently explicitly choose all correct responses as useful, without latency bound L. Latency quantiles use all correct completed responses, including slow ones. This supersedes the threshold-dependent parts of the older comparison prompt for experiment 02.

## Approach and scope

Two approaches were discussed: correctness-only service, or service within one common deadline. The author selected correctness-only. Report correct throughput G = correct/T, success fraction S = correct/sent, p50/p95/p99 of correct responses, errors/timeouts/rejections/pending, resource use and cost per correct response. Zero correct responses gives undefined per-response cost, not zero. No automatic delivery/sustainability verdict or binary feasible region without a stated criterion. Show observations across budgets and startup/OOM outcomes instead.

Reuse matrix, receipts, calendar, generator, source identity and platform adapters. Keep experiment 01 behavior and historical scenario parsing. Separate the shared calendar from experiment 01's required objectives; experiment 02 accepts old criteria/generator fields for compatibility but does not use them to judge the new comparison. New pilot omits them and pins the already verified PNG hash. Operational timeouts, finite drains, output correctness and evidence-integrity validation remain necessary.

One comparison module computes run-level means and deterministic percentile 95% bootstrap intervals, reusing the existing quantile function. One report command reads a saved run and checks existing receipt digests without Azure, source checkouts or rebuilding. The normal compare path writes the same aggregate after its existing per-cell reports. Derived output goes under analysis/; recorded inputs, receipts and raw evidence stay unchanged. One run folder is one cohort: no arbitrary cross-folder pooling.

JSON and CSV contain the reference-budget comparison and all observed budget/rate combinations, with planned/observed repetition counts, failure/incomplete counts, metric sample counts and missing values. A single observed run has a point estimate but no bootstrap interval. Latency percentiles are averaged across runs; individual requests never become replicates. Include original measurement identity and publication information.

## Execution boundary

Local implementation, tests and pilot preparation are approved. New Azure resources require a separate concrete Spot proposal with exact resource actions, sizes, maximum duration and costs. The pilot covers 11 configurations, one reference budget, one repetition. Collect Fn HTTP-trigger errors, tinyFaaS process names and cold-install/prefetch evidence during it. The final campaign still requires author approval of the recorded scenario. Do not edit the operator's existing paper sections or push/merge any repository.

The paper will subsequently need G/S and correctness-only efficiency definitions and a descriptive budget-grid presentation; it cannot retain claims about an L-qualified feasible region. This work records that required update without overwriting pending operator edits.

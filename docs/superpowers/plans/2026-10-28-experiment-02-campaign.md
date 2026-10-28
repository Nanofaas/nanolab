# Experiment 02 — plan 4: pilot, campaign and aggregation

Status: proposed; author approval required before implementation. Campaign additionally requires approval of the frozen scenario.
Prerequisite: plan 3 accepted or an explicit recorded exclusion; minor fixes verified.
Authority: nanofaas-paper/notes/experiment-02-platform-comparison-prompt.md.

## Tasks

1. Characterize existing scenario generation, cell ordering, cohort metrics and aggregation. Reuse existing experiment-01/02 facilities. Test missing samples, zero useful responses, failed startup/OOM and resume. Add only missing behavior; commit after each focused test passes.
2. Prepare one pilot scenario: every available configuration, one reference CPU/memory budget, one repetition. Include JVM/native and direct configurations as specified. Preserve one shared workload, canonical payload and fixed concurrency/queue policy. Record source commits, binaries, image digests, host/backend identities and measurement overhead. Estimate cells, VM hours and costs before requesting resource actions.
3. Run the approved pilot on Azure. Propose and record generator delivery, latency, correctness, errors/pending thresholds, open-loop calendar, drain, concurrency/queue limits, budget grid, seed and repetitions. Do not tune individual platforms after freezing. Obtain author approval of the immutable scenario before campaign execution.
4. Execute the frozen Azure campaign: reference-budget Table 3 first, then CPU × memory feasibility grid, with three repetitions unless the author approved a different count after the pilot. Record OOM and startup failures as outcomes. Distinguish an unmeasured cell from an infeasible measured cell. Resume completed cells without repetition and reject changed identities.
5. Test and produce mean estimates and percentile 95% bootstrap intervals by resampling runs, never individual requests. Preserve null cost-per-useful-response for zero useful responses. Apply the specification's arrival-cohort and drain definitions. Produce Table 3 and feasibility-region artifacts with scenario/source identities and explicit platform behavior/exclusions. Report the limitations of intervals based on few repetitions.
6. Run repository checks and real pinned-source contracts, then a fresh-context review. Preserve operator paper edits; agree on handling overlapping edits before writing results. Update methods/results/discussion only from actual artifacts and use the repository LaTeX workflow if latex-paper-en is unavailable. No push/merge without instruction.

## Acceptance

Approved frozen protocol and resource actions; all expected cells accounted for, including failures; reproducible aggregation with tests; no claims beyond evidence; paper builds with references checked; VM/resource status and costs reported.

# Illustrative soak report

**Synthetic documentation example only.** No build, runtime smoke, or P24 run
produced these entries. Paths below illustrate the evidence layout; they do not
link to existing experiment artifacts. No threshold here is a P24 policy.

```json
{
  "schema": "nanolab-soak-v1",
  "scope": "numerical-only",
  "purpose": "smoke",
  "status": "INCONCLUSIVE",
  "p24_qualified": false,
  "aborted": false,
  "results": [
    {
      "criterion_id": "control-plane.smoke-cgroup-budget",
      "status": "PASS",
      "reason": "Synthetic valid samples remain within the smoke container ceiling.",
      "evidence": ["samples.jsonl"]
    },
    {
      "criterion_id": "word-stats-java.smoke-rss-growth",
      "status": "INCONCLUSIVE",
      "reason": "Synthetic positive RSS growth needs attribution under the zero-growth smoke rule.",
      "evidence": ["samples.jsonl"]
    },
    {
      "criterion_id": "run-coverage",
      "status": "INCONCLUSIVE",
      "reason": "Numerical assessment does not establish complete protocol acceptance.",
      "evidence": ["evaluation-input.json"]
    }
  ],
  "warnings": [],
  "unverified": [
    "effective limits and frozen policy",
    "workload/prerequisite receipts",
    "complete diagnostics and attribution",
    "full-run acceptance"
  ]
}
```

| Role or gate | Example result | What it establishes |
| --- | --- | --- |
| Control plane cgroup budget | PASS | Only this numerical smoke ceiling, on valid synthetic observations |
| Java SDK natural RSS | INCONCLUSIVE | A residual still needs policy-bound attribution; heap recovery cannot waive it |
| JavaScript SDK | Not represented in this abbreviated example | A real all-role report cannot silently omit the role |
| Complete run coverage | INCONCLUSIVE | Numerical checks alone cannot certify workload, prerequisites, diagnostics, or P24 |

A real report must account for every configured role and criterion, preserve
labels, and reference timestamped observations tied to stable process identities.
It must distinguish the scheduled 5400-second P24 steady phase and 2100-second
natural drain from actual observed intervals. A short smoke cannot borrow those
durations from another run or become qualifying prerequisite evidence.

The P24 operator policy-source receipt must accompany the frozen configuration.
Its `path`, `sha256`, `size_bytes`, and `resolved_criteria_fingerprint` identify the
file and parsed criteria; application image/source fingerprints identify the
measured version separately. No dummy hash or invented completed receipt belongs
in real evidence.

On interruption, report `ABORTED` and retain demonstrated criterion failures.
On a valid demonstrated budget/retention/correctness violation, preserve `FAIL`
even if another criterion is inconclusive. Missing required samples remain
unavailable, never zero. On offline re-evaluation, write a new evaluation directory
and preserve the earlier report and attribution provenance.

An eventual complete smoke report may say that its smoke checks passed, but must
still state `p24_qualified: false`. A complete P24 report requires the separately
integrated full-run evaluator and all required evidence; this example makes no
such claim. The [operator guide](soak.md) lists the current integration limits.

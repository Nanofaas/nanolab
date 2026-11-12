# Receipts and cold forecasts

User-authorized native implementation, following the agreed log-based design.

The Rust function SDK emits a versioned structured receipt on physical handler release. Existing k6 results remain client evidence; Prometheus remains aggregate monitoring. Fluent Bit and Loki carry receipts. Docker journald logging on owned experiment VMs preserves records independently of container deletion. Workbench mounts the journal and collects it; Nanolab correlates receipts with original/terminal execution IDs and rejects conflicting or incomplete evidence. No invocation IDs become Loki labels. Existing non-journal deployments keep working.

A forecast without sufficient traffic history is MISSING, never fabricated zero. Before a usable forecast exists, NanoFaaS uses bounded local admission and sends overflow to cloud. An actually observed idle period after learning traffic may still yield zero. The fallback must respect memory, generation and replica readiness and must not offer speculative capacity to neighbors.

Alternatives: separate receipt database rejected (duplicates Loki); polling runtime histories rejected for experiments (history dies with replica); merely delaying deletion rejected (timing does not prove collection). Journald is a host facility with bounded retention, not an exactly-once ledger. Collector outages beyond retention remain explicit evidence gaps.

Tests cover release/timeout semantics, duplicate/conflicting receipts, removal with collector stopped then replay, insufficient data versus measured zero, local admission/overflow, and transition to forecasts. Full five-edge workflows remain bounded to five edge plus cloud.

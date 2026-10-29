# Three existing one-shot experiments, one launcher

On the operator host, with the six historical input paths in a JSON configuration:

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-sequence.py \
  /home/michele/Documenti/one-shot-sequence-inputs.json \
  --output /home/michele/Documenti/one-shot-sequence-$(date +%Y%m%d-%H%M%S)
```

The launcher builds the current clean NanoFaaS checkout once and prepares all
three freezes before starting any VM. Then it runs, in this exact order:

1. Step load, ORACLE, 60-second decisions.
2. Historical continuous arrival calendar, ORACLE, 60-second decisions.
3. The identical continuous calendar, ARI (10-second windows, order 6),
   60-second decisions.

Each run keeps the original 54 minutes of load and 12 minutes of warmup. A step
that originally lasted six minutes still lasts six minutes; only the allocation
is recalculated every minute. Rates, payloads, total duration and the evenly
spaced step arrivals are preserved. The original reference plans are replicated
onto the finer decision grid; they are not new optimization results. Continuous
arrivals and reference means use the existing verified one-minute preparation.

The JSON configuration has these path keys: `runtimeSource`, `stepTuning`,
`continuousTuning`, `continuousParentFreeze`, `observations`, `physicalSamples`.
Relative paths resolve beside the JSON. The launcher verifies the historical
sample hash, relocates only its source path in a new observations copy, and uses
actual historical statistics with the existing explicitly unqualified observation
protocol. It does not turn historical means into newly qualified measurements.
All archives and original inputs remain unchanged.

Add `--check` to validate inputs without creating the output, building images,
or starting VMs. Output must be new. `sequence.json` records the input paths and
runtime commit. Each run has its own `*-freeze`, `*-run` and `*-archive` directory;
`sequence-status.json` records progress. The next run starts only after the
previous owned workflow, archive and cleanup return successfully. Nonzero exit
or interruption stops the sequence. The frozen runner retains its existing
supervision and cleanup behavior.

The runtime uses the queue, admission and latency instrumentation already in the
current source. The script does not modify resource quotas, inputs, or workloads.

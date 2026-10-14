# Offline one-minute inputs for the continuous ORACLE experiment

`one-shot-rewindow.py` reuses the saved continuous arrival calendar and spline
coefficients. It integrates each minute to build a new oracle, without
resampling requests or starting a campaign. Existing workflows and scripts are
unchanged. The original `one-shot-observe.py` does not yet accept this schema-v2
preparation as an executable campaign freeze.

Run with Python 3.12+ through the script's pinned NumPy/SciPy environment:

```bash
uv run --script scripts/one-shot-rewindow.py \
  --freeze /path/to/continuous-parent-freeze \
  --output /path/to/new-offline-inputs \
  --period-seconds 60 \
  --reference /path/to/original-python-model \
  --reference-python /path/to/original-model-environment/bin/python
```

`--reference` and `--reference-python` are supplied together. Omitting both
produces the trace/calendar/means only. An existing output directory is rejected;
a failed simulation retains its input and log without retry. Historical source
inventories and all pinned reference Python files are checked, and results keep
a hash inventory. No sampler, generator, VM or container is launched.

The original 54-minute measurement and 12-minute warmup keep the same offsets.
Fifty-four one-minute windows replace nine six-minute windows. The original
spline time scale is explicitly 360 seconds even though decisions use 60 seconds.

The mathematical means are saved in `means.json`. `means.csv` includes integrated
masses, actual integer counts and signed errors after nearest 0.05 requests/s
quantization, with midpoint ties upward. Rounding affects only ORACLE/model
input: it never alters the arrival calendar. Sub-quantum rates may round to zero.
The protocol quantum and eight auction rounds are inherited from the parent.

`selected.json` holds all original-model results, using the same independent
per-epoch local optimization and fixed-round convention as the earlier run.
`reference-environment.json` records the actual model interpreter/packages.
The preceding trace-only verification and final `oracle-verification.json` are
separate; `runtimeReady` deliberately remains false.

The prepared campaign bundle and readable routing/memory CSVs live in the
private `Nanofaas/dfaas-documents` repository, under
`experiments/one-shot-continuous-60s-2026-10-14/`.
Runtime timing policy is deferred: the inherited maximum auction 60 s plus
preparation 10 s does not satisfy the current 60 s period check. No timeout or
native policy has been changed to bypass that check.

Tests reuse the existing environments; no package installation is needed:

```bash
ONE_SHOT_CURVE_PYTHON=/path/to/scipy-environment/bin/python \
ONE_SHOT_REFERENCE_ROOT=/path/to/original-python-model \
ONE_SHOT_REFERENCE_PYTHON=/path/to/original-model-environment/bin/python \
uv run --frozen --all-packages --all-groups pytest \
  -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/one_shot/test_rewindow.py \
  packages/nanolab/tests/one_shot/test_reference_load.py
```

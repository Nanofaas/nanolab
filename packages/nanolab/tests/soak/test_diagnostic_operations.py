"""The operation vocabulary is one declaration, not five copies of a set.

`histogram` and `jfr` were accepted by the config literal, by both host-side
operation sets and by the adapter's command table, while the runtime gate
refused to provision either and the worker rejected both at its own validation.
Every test of them drove a hand-written fake helper, so no test ever crossed the
production gate -- the same shape as the six blockers the first real run found.
These tests tie the declarations together, and hold the text-reading validators
against real JDK 25 output.

The fixtures below are host captures, which is what these validators can be
tested against here. Both readings were also confirmed through the real
provisioner and the pinned helper against a container JVM as PID 1:
`native_memory` captured categories with the flag and reported
"is not a complete reading" with the flag absent, and `histogram` captured
105 KB of real rows. That harness is scratch, not checked in.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import get_args

import pytest

from nanolab.config.soak import DiagnosticOperation
from nanolab.tasks.soak import diagnostic_exec
from nanolab.tasks.soak.diagnostics import (
    LOCAL_HELPER_OPERATIONS,
    OPERATION_VOCABULARY,
    supported_operations,
)

# Real capture: `jcmd <pid> VM.native_memory summary` on JDK 25.0.4 (build
# 25.0.4+7-1-24.04-Ubuntu) started with `-XX:NativeMemoryTracking=summary
# -XX:+UseSerialGC -XX:TieredStopAtLevel=1`. The middle categories are elided
# here; the header, the Total line and the two categories below are verbatim,
# pid header included because that is what the worker returns. Reproduce with a
# single-file Java program that sleeps. Never hand-write this text.
NMT_SUMMARY = (
    "322850:\n"
    "\n"
    "Native Memory Tracking:\n"
    "\n"
    "(Omitting categories weighting less than 1KB)\n"
    "\n"
    "Total: reserved=32787251KB, committed=2043467KB\n"
    "       malloc: 12443KB #43541, peak=11678KB #43543\n"
    "       mmap:   reserved=32774808KB, committed=2031024KB\n"
    "\n"
    "-                 Java Heap (reserved=31455232KB, committed=1994752KB)\n"
    "                            (mmap: reserved=31455232KB, committed=1994752KB,"
    " at peak)\n"
    "\n"
    "-                     Class (reserved=1048768KB, committed=1408KB)\n"
    "                            (classes #2863)\n"
)

# Real capture: the same JVM without the flag. This is the whole reason the
# content decides completion instead of the exit status -- jcmd exits 0 here.
NMT_DISABLED = "323188:\nNative memory tracking is not enabled\n"

# Real capture: `jcmd <pid> VM.native_memory baseline`, same JDK and flags. The
# acknowledgement is the entire output; there is no category report.
NMT_BASELINE = "416319:\nBaseline taken\n"

# Real capture: `jcmd <pid> VM.native_memory summary.diff`, same JDK and flags,
# taken right after the baseline above. Rows elided. NMT's own bookkeeping shows
# up as +1KB, which is why a real diff is never exactly zero.
NMT_DIFF = (
    "416319:\n"
    "\n"
    "Native Memory Tracking:\n"
    "\n"
    "(Omitting categories weighting less than 1KB)\n"
    "\n"
    "Total: reserved=32981736KB +1KB, committed=2042256KB +1KB\n"
    "\n"
    "malloc=7236KB +1KB #49355 +18\n"
    "\n"
    "mmap: reserved=32974500KB, committed=2035020KB\n"
    "\n"
    "-                 Java Heap (reserved=31455232KB, committed=1994752KB)\n"
)

# Real capture: the same command on a JVM that never took one. Exits 0.
NMT_DIFF_NO_BASELINE = "415190:\nNo baseline for comparison\n"

# Real capture, taken by the validation script through the helper: the same
# command after a baseline, on a JVM that had released memory since. NMT signs
# the delta, so a validator accepting only `+N` refuses exactly the reading a
# shrink spike exists to find.
NMT_DIFF_RELEASED = (
    "1:\n"
    "\n"
    "Native Memory Tracking:\n"
    "\n"
    "(Omitting categories weighting less than 1KB)\n"
    "\n"
    "Total: reserved=32801434KB -6631KB, committed=2056834KB -6631KB\n"
    "\n"
    "malloc=22546KB -6631KB #50045 -196\n"
    "\n"
    "mmap: reserved=32778888KB, committed=2034288KB\n"
    "\n"
    "-                 Java Heap (reserved=31455232KB, committed=1994752KB)\n"
    "                            (mmap: reserved=31455232KB, committed=1994752KB)\n"
)

# Real capture: `VM.native_memory baseline` without the flag. Exits 0.
NMT_BASELINE_DISABLED = "418041:\nNative memory tracking is not enabled\n"

# Real capture: `jcmd <pid> GC.class_histogram`, same JDK. Rows elided.
HISTOGRAM = (
    "327718:\n"
    " num     #instances         #bytes  class name (module)\n"
    "-------------------------------------------------------\n"
    "   1:         15347        9149504  [B (java.base@25.0.4)\n"
    "   2:          3347         431736  java.lang.Class (java.base@25.0.4)\n"
    "Total         68280       11537656\n"
)


def worker():
    """Load the script asset directly, independently of pytest import mode."""
    path = Path(__file__).parents[2] / "assets/soak/diagnostic-worker.py"
    spec = importlib.util.spec_from_file_location("operations_worker", path)
    module = importlib.util.module_from_spec(spec)  # pyright: ignore[reportArgumentType]
    spec.loader.exec_module(module)  # pyright: ignore[reportOptionalMemberAccess]
    return module


def test_the_config_literal_and_the_host_side_sets_agree():
    """A name the config accepts must not be one the executor refuses."""
    assert set(get_args(DiagnosticOperation)) == OPERATION_VOCABULARY
    assert diagnostic_exec.OPERATION_VOCABULARY == OPERATION_VOCABULARY


def test_every_provisionable_operation_is_in_the_vocabulary():
    provisionable = set().union(*LOCAL_HELPER_OPERATIONS.values())
    assert provisionable
    assert provisionable.issubset(OPERATION_VOCABULARY)


def test_the_worker_dispatches_exactly_what_the_host_provisions():
    """The two sets live in different languages and must not drift apart.

    Provisioning admits a run from the host set; the helper answers from its
    own. A name in one and not the other is a run that dies at its first
    capture, which is how `histogram` and `jfr` stayed unreachable.
    """
    provisionable = set().union(*LOCAL_HELPER_OPERATIONS.values())

    assert set(worker().SUPPORTED_OPERATIONS) == provisionable


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        (
            "jvm",
            {
                "gc",
                "histogram",
                "heap_dump",
                "native_memory",
                "native_memory_baseline",
                "native_memory_diff",
            },
        ),
        ("node", {"gc", "heap_dump"}),
    ],
)
def test_the_gate_answers_per_runtime(runtime, expected):
    assert supported_operations(runtime) == expected
    # An unknown runtime provisions nothing rather than falling back to a set.
    assert supported_operations("wasm") == frozenset()


@pytest.mark.parametrize(
    "operation",
    ["histogram", "native_memory", "native_memory_baseline", "native_memory_diff"],
)
def test_the_jvm_only_text_readings_are_absent_for_node(operation):
    """A node target has no jcmd, so the worker could not run these at all."""
    assert operation not in supported_operations("node")


def test_real_nmt_summary_is_a_complete_reading():
    assert worker().native_memory_completed(NMT_SUMMARY) is True


def test_disabled_nmt_is_never_a_complete_reading():
    """Jcmd exits 0 and says this, so a capture must not record it as evidence."""
    assert worker().native_memory_completed(NMT_DISABLED) is False


def test_real_histogram_is_a_complete_reading():
    assert worker().histogram_completed(HISTOGRAM) is True


@pytest.mark.parametrize(
    ("validator", "text"),
    [
        ("native_memory_completed", NMT_SUMMARY.rsplit("Total:", 1)[0]),
        ("native_memory_diff_completed", NMT_DIFF.rsplit("Total:", 1)[0]),
        ("histogram_completed", HISTOGRAM.rsplit("Total", 1)[0]),
        ("native_memory_baseline_taken", "416319:\n"),
        ("native_memory_completed", ""),
        ("native_memory_diff_completed", ""),
        ("histogram_completed", ""),
    ],
)
def test_a_cut_read_is_never_a_complete_reading(validator, text):
    """A truncated read must be unresolved, never a short but valid artifact."""
    assert getattr(worker(), validator)(text) is False


def test_real_nmt_baseline_acknowledgement_is_a_complete_reading():
    assert worker().native_memory_baseline_taken(NMT_BASELINE) is True


def test_a_baseline_on_a_flagged_off_jvm_is_never_a_reading():
    """`jcmd` exits 0 here, so nothing but the content can refuse it."""
    assert worker().native_memory_baseline_taken(NMT_BASELINE_DISABLED) is False


def test_real_nmt_diff_is_a_complete_reading():
    assert worker().native_memory_diff_completed(NMT_DIFF) is True


def test_a_diff_without_a_baseline_is_never_a_reading():
    assert worker().native_memory_diff_completed(NMT_DIFF_NO_BASELINE) is False


def test_a_diff_that_released_memory_is_still_a_reading():
    """A signed delta is the answer, not a malformed line.

    The validation script caught this one: the first real diff taken through the
    helper reported `-6631KB`, and a validator written around the growth case
    refused it as unreadable.
    """
    assert worker().native_memory_diff_completed(NMT_DIFF_RELEASED) is True


def test_a_diff_with_nothing_to_report_is_still_a_reading():
    """The deltas are optional, and their absence is the answer worth having.

    A run whose native memory did not grow is the outcome that closes the leak
    question. Requiring a `+N` suffix would record it as an unreadable capture
    and leave the operator concluding the opposite of what was measured.

    The diff carries two kinds of delta -- bytes (`+1KB`) and the allocation
    counts NMT tracks alongside them (`#49355 +18`) -- and a reading with
    nothing to report drops both, so this fixture drops both.
    """
    no_deltas = "\n".join(
        re.sub(r" [+-]\d+(?:KB?)?", "", line) for line in NMT_DIFF.splitlines()
    )
    assert " +" not in no_deltas and " -" not in no_deltas
    assert worker().native_memory_diff_completed(no_deltas) is True


def test_the_summary_validator_does_not_accept_a_diff():
    """A mode mix-up must not pass as a complete reading of the other mode."""
    assert worker().native_memory_completed(NMT_DIFF) is False


def test_the_worker_rejects_an_operation_it_cannot_dispatch():
    module = worker()
    request = {
        "kind": "execute",
        "operation": "not_an_operation",
        "request_id": "abc",
        "max_bytes": 4096,
        "timeout_s": 1,
    }
    with pytest.raises(ValueError, match="unprovisioned"):
        module.validate_request(request)
    module.validate_request({**request, "operation": "native_memory"})

import pytest

from nanolab.tasks.heap_analysis.native import (
    LARGE_MAPPING_BYTES,
    parse_heap_info,
    parse_smaps,
    residency,
    summarize,
)

# Real capture: `jcmd <pid> GC.heap_info` on JDK 25.0.4 (build
# 25.0.4+7-1-24.04-Ubuntu), G1 at -Xms256m -Xmx1g. G1 is what some
# control-plane variants build with; Serial below is the default. Which one
# applies is decided by the IMAGE's JVM_TUNING, never by the scenario, so both
# shapes need covering. Reproduce this one with a single-file Java program that
# sleeps, run as `java -Xms256m -Xmx1g -XX:+UseG1GC Prog.java`, then
# `jcmd <pid> GC.heap_info` against it. Never hand-write this text: an invented
# fixture is what let a parser matching no real JDK 25 output pass review.
HEAP_INFO = (
    "garbage-first heap   total reserved 1048576K, committed 264192K, "
    "used 27268K [0x00000000c0000000, 0x0000000100000000)\n"
    " region size 1024K, 26 young (26624K), 0 survivors (0K)\n"
)

# What the helper actually receives: `jcmd` writes the target pid header
# before the heap line, and the worker returns the child's stdout verbatim.
WORKER_HEAP_INFO = "1:\n" + HEAP_INFO

# Real capture, and the shape production actually produces. The control-plane
# image is built with `-XX:+UseSerialGC -XX:TieredStopAtLevel=1`
# (nanolab/images/control_plane_variants.py), so its GC.heap_info prints one
# line per generation instead of a G1 summary. Taken verbatim from a real run's
# evidence/native/before-baseline-heap-info.txt, pid header included because
# that header is what the worker returns.
SERIAL_HEAP_INFO = (
    "1:\n"
    "DefNew     total 20992K, used 2955K "
    "[0x00000000d3200000, 0x00000000d48c0000, 0x00000000e2150000)\n"
    " eden space 18688K,   3% used "
    "[0x00000000d3200000, 0x00000000d32a2c88, 0x00000000d4440000)\n"
    " from space 2304K, 100% used "
    "[0x00000000d4440000, 0x00000000d4680000, 0x00000000d4680000)\n"
    " to   space 2304K,   0% used "
    "[0x00000000d4680000, 0x00000000d4680000, 0x00000000d48c0000)\n"
    "Tenured    total 46480K, used 31915K "
    "[0x00000000e2150000, 0x00000000e4eb4000, 0x0000000100000000)\n"
    " the  space 46480K,  68% used "
    "[0x00000000e2150000, 0x00000000e407af28, 0x00000000e4eb4000)\n"
)

# Real capture: the same JDK with -XX:+UseParallelGC. It has neither a
# `garbage-first heap` line nor a DefNew/Tenured pair, so nothing parses. This
# is the shape a collector this code does not know would take.
PARALLEL_HEAP_INFO = (
    "PSYoungGen      total 76288K, used 31570K "
    "[0x00000000eab00000, 0x00000000f0000000, 0x0000000100000000)\n"
    " eden space 65536K, 48% used "
    "[0x00000000eab00000,0x00000000ec9d49c8,0x00000000eeb00000)\n"
    " from space 10752K, 0% used "
    "[0x00000000ef580000,0x00000000ef580000,0x00000000f0000000)\n"
    " to   space 10752K, 0% used "
    "[0x00000000eeb00000,0x00000000eeb00000,0x00000000ef580000)\n"
    "ParOldGen       total 175104K, used 1156K "
    "[0x00000000c0000000, 0x00000000cab00000, 0x00000000eab00000)\n"
    " object space 175104K, 0% used "
    "[0x00000000c0000000,0x00000000c0121178,0x00000000cab00000)\n"
)

SMAPS = """\
7f0000000000-7f0004000000 rw-p 00000000 00:00 0
Size:              65536 kB
Rss:               32768 kB
Pss:               32768 kB
7f0010000000-7f0010001000 r--p 00000000 08:01 1234    /usr/lib/libc.so.6
Size:                  4 kB
Rss:                   4 kB
Pss:                   2 kB
7f0020000000-7f0022000000 ---p 00000000 00:00 0
Size:              32768 kB
Rss:                   0 kB
Pss:                   0 kB
"""


def test_heap_info_is_normalized_to_bytes():
    parsed = parse_heap_info(HEAP_INFO)
    assert parsed["heap"] == {"committed": 264192 * 1024, "used": 27268 * 1024}
    # JDK 25 GC.heap_info prints no Metaspace line: absent, never zero.
    assert parsed["metaspace"] is None


@pytest.mark.parametrize("text", [HEAP_INFO, WORKER_HEAP_INFO], ids=["stdout", "jcmd"])
def test_real_g1_capture_round_trips_to_available_bytes(text):
    """The pid header `jcmd` writes must not stop the heap line parsing."""
    block = summarize({"heap_info": text, "errors": {}})["heap_info"]
    assert block["available"] is True
    assert block["heap"]["committed"] == 264192 * 1024
    assert block["heap"]["used"] == 27268 * 1024
    assert block["metaspace"] is None


def test_heap_info_marks_unrecognized_output_unavailable_instead_of_zero():
    parsed = parse_heap_info("Shenandoah Heap\n")
    assert parsed["heap"] is None
    assert parsed["metaspace"] is None


def test_present_but_unrecognized_heap_info_publishes_an_error():
    """A format change must be visible, not a silent unavailable."""
    block = summarize({"heap_info": PARALLEL_HEAP_INFO, "errors": {}})["heap_info"]
    assert block["available"] is False
    assert "unrecognized" in block["error"]
    assert "heap" not in block
    assert block.get("metaspace") is None


def test_real_serial_capture_round_trips_to_available_bytes():
    """SerialGC is the control-plane default, so this is the production shape.

    Both generations are summed, and `total` is each space's committed
    capacity -- the quantity G1 spells `committed`. The first real run recorded
    Serial and published an "unrecognized" error, because only G1 was matched.
    """
    block = summarize({"heap_info": SERIAL_HEAP_INFO, "errors": {}})["heap_info"]

    assert block["available"] is True
    assert block["heap"]["committed"] == (20992 + 46480) * 1024
    assert block["heap"]["used"] == (2955 + 31915) * 1024
    assert block["metaspace"] is None


@pytest.mark.parametrize("missing", ["DefNew", "Tenured"])
def test_a_partial_serial_capture_publishes_no_heap_total(missing):
    """Half the generations is an incomplete reading, not half a heap."""
    text = "\n".join(
        line for line in SERIAL_HEAP_INFO.splitlines() if not line.startswith(missing)
    )
    assert parse_heap_info(text)["heap"] is None


def test_smaps_reports_each_mapping_and_keeps_categories_separate():
    parsed = parse_smaps(SMAPS)
    assert parsed["mappings"] == 3
    assert parsed["anonymous"] == {
        "size": (65536 + 32768) * 1024,
        "rss": 32768 * 1024,
        "pss": 32768 * 1024,
    }
    assert parsed["file"] == {"size": 4 * 1024, "rss": 4 * 1024, "pss": 2 * 1024}


def test_smaps_reports_large_mappings_individually_without_naming_an_owner():
    parsed = parse_smaps(SMAPS)
    large = parsed["large_anonymous_mappings"]
    assert large["count"] == 2
    assert large["size"] == (65536 + 32768) * 1024
    assert sorted(item["size"] for item in large["mappings"]) == [
        32768 * 1024,
        65536 * 1024,
    ]
    assert LARGE_MAPPING_BYTES == 33554432
    assert "arena" not in repr(parsed)


def test_residency_keeps_kernel_categories_and_leaves_missing_fields_absent():
    status = "Name:\tjava\nRssAnon:\t 32768 kB\nRssFile:\t  4096 kB\n"
    rollup = "Pss_Anon:\t 32768 kB\nPss_File:\t  2048 kB\n"
    values = residency(status, rollup)
    assert values["RssAnon"] == 32768 * 1024
    assert values["RssFile"] == 4096 * 1024
    assert "RssShmem" not in values
    assert values["Pss_Anon"] == 32768 * 1024
    assert "Pss_Shmem" not in values


def test_summary_publishes_no_smaps_totals_when_the_read_was_incomplete():
    block = summarize(
        {
            "status": "RssAnon:\t 32768 kB\n",
            "smaps_rollup": "Pss_Anon:\t 32768 kB\n",
            "smaps": None,
            "heap_info": HEAP_INFO,
            "errors": {"smaps": "ValueError: procfs evidence exceeds read bound"},
        }
    )
    assert block["smaps"] == {
        "available": False,
        "error": "ValueError: procfs evidence exceeds read bound",
    }
    assert block["heap_info"]["available"] is True
    assert block["residency"]["RssAnon"] == 32768 * 1024


def test_summary_marks_a_source_unavailable_when_it_was_never_requested():
    block = summarize({"status": "RssAnon:\t 4 kB\n", "smaps_rollup": "", "errors": {}})
    assert block["smaps"]["available"] is False
    assert block["heap_info"]["available"] is False
    assert "error" not in block["smaps"]
    # Never requested is not a failure: it stays error-free.
    assert "error" not in block["heap_info"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "unrecognized output",
        SMAPS.rsplit("Pss:", 1)[0],
        "1000-2000 rw-p 0 00:00 0\nSize: 4 kB\nRss: 4 kB\n",
    ],
)
def test_incomplete_smaps_never_produces_totals(text):
    block = summarize({"smaps": text, "errors": {}})["smaps"]
    assert block["available"] is False
    assert "anonymous" not in block
    assert "large_anonymous_mappings" not in block


@pytest.mark.parametrize(
    ("permissions", "path", "category"),
    [
        ("rw-p", "[heap]", "anonymous"),
        ("rw-p", "[anon:java-heap]", "anonymous"),
        ("rw-s", "[anon_shmem:shared]", "shared_memory"),
        ("rw-s", "/dev/shm/shared", "shared_memory"),
        ("rw-s", "", "shared_memory"),
        ("r-xp", "[vdso]", "unknown"),
        ("rw-p", "/tmp/private-file", "file"),
    ],
)
def test_smaps_backing_categories_are_explicit(permissions, path, category):
    raw = (
        f"10000000-14000000 {permissions} 0 00:00 0 {path}\n"
        "Size: 65536 kB\nRss: 4 kB\nPss: 4 kB\nAnonymous: 4 kB\n"
    )
    parsed = parse_smaps(raw)
    assert parsed[category]["rss"] == 4096
    # A file VMA containing anonymous COW pages remains a file VMA;
    # process page residency comes from status/rollup, not this category.
    assert parsed["mapping_details"][0]["backing"] == category
    assert parsed["large_anonymous_mappings"]["count"] == (category == "anonymous")


@pytest.mark.parametrize(
    ("size_kb", "qualifies"), [(32767, False), (32768, True), (32769, True)]
)
def test_large_mapping_boundary(size_kb, qualifies):
    end = 0x10000000 + size_kb * 1024
    raw = f"10000000-{end:x} ---p 0 00:00 0\nSize: {size_kb} kB\nRss: 0 kB\nPss: 0 kB\n"
    assert parse_smaps(raw)["large_anonymous_mappings"]["count"] == int(qualifies)

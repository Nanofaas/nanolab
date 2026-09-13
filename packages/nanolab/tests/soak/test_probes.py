import pytest


def test_labels_and_timestamps_are_preserved():
    from nanolab.tasks.soak.probes import parse_exposition

    rows = parse_exposition(
        'jvm_memory_used_bytes{area="heap"} 10 123\n'
        'jvm_memory_used_bytes{area="nonheap"} 20\n'
    )
    assert rows == (
        ("jvm_memory_used_bytes", (("area", "heap"),), 10.0),
        ("jvm_memory_used_bytes", (("area", "nonheap"),), 20.0),
    )


def test_escaped_labels():
    from nanolab.tasks.soak.probes import parse_exposition

    assert parse_exposition(r'm{a="a\n\"\\",b="x"} 1')[0][1] == (
        ("a", 'a\n"\\'),
        ("b", "x"),
    )


@pytest.mark.parametrize(
    "text",
    [
        'm{a="x",a="y"} 1',
        'm{a="x"} NaN',
        "m +Inf",
        "m 1e999",
        'm{a="x} 1',
        'm{a="\\t"} 1',
        "m 1 junk",
        "m 1\nm 2",
    ],
)
def test_bad_exposition_is_not_zero(text):
    from nanolab.tasks.soak.probes import parse_exposition

    with pytest.raises(
        ValueError,
        match=(
            r"duplicate series at line 2|malformed exposition at line 1|"
            r"malformed or duplicate label|nonfinite exposition at line 1"
        ),
    ):
        parse_exposition(text)


def test_exposition_bounds():
    from nanolab.tasks.soak.probes import parse_exposition

    with pytest.raises(ValueError, match="measurement text limit exceeded"):
        parse_exposition("#" * (1024 * 1024 + 1))


def test_procfs_preserves_rss_pss_and_missing_values():
    from nanolab.tasks.soak.probes import parse_procfs_memory

    assert parse_procfs_memory("VmRSS: 12 kB\n", "Pss: 7 kB\n") == {
        "process_rss_bytes": 12288,
        "process_pss_bytes": 7168,
    }
    assert parse_procfs_memory("Name: java\n", None) == {
        "process_rss_bytes": None,
        "process_pss_bytes": None,
    }


@pytest.mark.parametrize("text", ["VmRSS: -1 kB", "VmRSS: 1 MB", "VmRSS: bad kB"])
def test_invalid_procfs_values(text):
    from nanolab.tasks.soak.probes import parse_procfs_memory

    with pytest.raises(ValueError, match="invalid procfs VmRSS"):
        parse_procfs_memory(text, None)

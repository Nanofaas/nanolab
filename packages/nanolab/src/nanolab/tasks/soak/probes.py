"""Bounded parsers for distinct process and exposition measurements.

Transport adapters must turn parser errors into unavailable samples, never zero.
These parsers deliberately do not infer registry population from exposition.
"""

import math
import re

_MAX_TEXT = 1024 * 1024
_MAX_ROWS = 10000
_ROW = re.compile(
    r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+"
    r"([^\s]+)(?:\s+([+-]?\d+))?\s*"
)
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\[\\"n])*)"\s*')
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _bounded(text: str) -> None:
    if len(text) > _MAX_TEXT:
        raise ValueError("measurement text limit exceeded")


def _labels(text: str) -> tuple[tuple[str, str], ...]:
    labels: dict[str, str] = {}
    position = 0
    while text[position:].strip():
        match = _LABEL.match(text, position)
        if match is None or match[1] in labels:
            raise ValueError("malformed or duplicate label")
        labels[match[1]] = re.sub(
            r'\\([\\"n])', lambda item: "\n" if item[1] == "n" else item[1], match[2]
        )
        if len(labels) > 128:
            raise ValueError("label limit exceeded")
        position = match.end()
        if position == len(text):
            break
        if text[position] != ",":
            raise ValueError("malformed label separator")
        position += 1
    return tuple(sorted(labels.items()))


def parse_exposition(
    text: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...], float], ...]:
    """Parse finite Prometheus text samples without collapsing label sets.

    Optional integer timestamps are accepted; observer timing remains authoritative.
    Unsupported/malformed input invalidates this scrape rather than hiding rows.
    """
    _bounded(text)
    rows = []
    seen = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ROW.fullmatch(line)
        if match is None or _NUMBER.fullmatch(match[3]) is None:
            raise ValueError(f"malformed exposition at line {line_number}")
        value = float(match[3])
        if not math.isfinite(value):
            raise ValueError(f"nonfinite exposition at line {line_number}")
        labels = _labels(match[2] or "")
        identity = (match[1], labels)
        if identity in seen:
            raise ValueError(f"duplicate series at line {line_number}")
        seen.add(identity)
        rows.append((match[1], labels, value))
        if len(rows) > _MAX_ROWS:
            raise ValueError("exposition row limit exceeded")
    return tuple(rows)


def _proc_value(text: str | None, field: str) -> int | None:
    if text is None:
        return None
    _bounded(text)
    found = None
    for line in text.splitlines():
        if line.startswith(field + ":"):
            match = re.fullmatch(re.escape(field) + r":\s+(\d+)\s+kB\s*", line)
            if match is None or found is not None:
                raise ValueError(f"invalid procfs {field}")
            found = int(match[1]) * 1024
    return found


def parse_procfs_memory(
    status: str | None, smaps_rollup: str | None
) -> dict[str, int | None]:
    """Return RSS and PSS in bytes; inaccessible/absent fields remain missing."""
    return {
        "process_rss_bytes": _proc_value(status, "VmRSS"),
        "process_pss_bytes": _proc_value(smaps_rollup, "Pss"),
    }

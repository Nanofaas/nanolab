"""Integer reference results for the five distinct Rust calibration handlers."""

from __future__ import annotations

MASK = (1 << 64) - 1
MULTIPLIER = 6364136223846793005
INCREMENT = 1442695040888963407

_FIELDS = {
    "one-shot-arithmetic": {"iterations", "seed"},
    "one-shot-sort": {"items", "passes", "seed"},
    "one-shot-memory": {"working_set_bytes", "passes", "seed"},
    "one-shot-matrix": {"dimension", "repetitions", "seed"},
    "one-shot-mixed": {
        "arithmetic_iterations",
        "sort_items",
        "sort_passes",
        "working_set_bytes",
        "memory_passes",
        "matrix_dimension",
        "matrix_repetitions",
        "seed",
    },
}


WORKLOAD_NAMES = frozenset(_FIELDS)


def _rotate(value: int, bits: int = 5) -> int:
    return ((value << bits) | (value >> (64 - bits))) & MASK


def _validate(name: str, payload: dict) -> None:
    if (
        name not in _FIELDS
        or set(payload) != _FIELDS[name]
        or any(
            type(value) is not int or value < 0 or value > MASK
            for value in payload.values()
        )
    ):
        raise ValueError("invalid one-shot workload input or identity")
    if name == "one-shot-arithmetic":
        valid = payload["iterations"] <= 1_000_000_000
    elif name == "one-shot-sort":
        valid = (
            payload["items"] <= 1_000_000
            and payload["passes"] <= 1_000_000
            and payload["items"] * payload["passes"] <= 100_000_000
        )
    elif name == "one-shot-memory":
        valid = (
            payload["working_set_bytes"] <= 64 * 1024 * 1024
            and payload["passes"] <= 1_000_000
            and payload["working_set_bytes"] * payload["passes"] <= 1_000_000_000
        )
    elif name == "one-shot-matrix":
        valid = (
            payload["dimension"] <= 256
            and payload["repetitions"] <= 1_000_000
            and payload["dimension"] ** 3 * payload["repetitions"] <= 1_000_000_000
        )
    else:
        for sub_name, sub_input in _mixed_parts(payload):
            _validate(sub_name, sub_input)
        return
    if not valid:
        raise ValueError("one-shot workload input exceeds runtime bounds")


def _mixed_parts(payload: dict) -> list[tuple[str, dict]]:
    seed = payload["seed"]
    return [
        (
            "one-shot-arithmetic",
            {"iterations": payload["arithmetic_iterations"], "seed": seed},
        ),
        (
            "one-shot-sort",
            {
                "items": payload["sort_items"],
                "passes": payload["sort_passes"],
                "seed": seed,
            },
        ),
        (
            "one-shot-memory",
            {
                "working_set_bytes": payload["working_set_bytes"],
                "passes": payload["memory_passes"],
                "seed": seed,
            },
        ),
        (
            "one-shot-matrix",
            {
                "dimension": payload["matrix_dimension"],
                "repetitions": payload["matrix_repetitions"],
                "seed": seed,
            },
        ),
    ]


def expected_output(function: str, payload: dict) -> int:
    """Validate before allocating; retain all 64 bits without floating conversion."""
    name = function.removesuffix("-rust")
    _validate(name, payload)
    seed = payload["seed"]
    result = seed
    if name == "one-shot-arithmetic":
        for index in range(payload["iterations"]):
            result = (result * MULTIPLIER + (index ^ INCREMENT)) & MASK
    elif name == "one-shot-sort":
        values = []
        state = seed
        for index in range(payload["items"]):
            state = (state * MULTIPLIER + (index ^ INCREMENT)) & MASK
            values.append(state)
        for number in range(payload["passes"]):
            values = [(v * MULTIPLIER + number) & MASK for v in sorted(values)]
        for value in values:
            result = _rotate(result) ^ value
    elif name == "one-shot-memory":
        values = bytearray(
            (seed + index) & 255 for index in range(payload["working_set_bytes"])
        )
        for number in range(payload["passes"]):
            for index, value in enumerate(values):
                value = (value + number + 1) & 255
                values[index] = value
                result = _rotate(result) ^ value
    elif name == "one-shot-matrix":
        dim = payload["dimension"]
        left = [
            [(seed + row * dim + col) & 65535 for col in range(dim)]
            for row in range(dim)
        ]
        right = [
            [(_rotate(seed, 17) + 3 * (row * dim + col)) & 65535 for col in range(dim)]
            for row in range(dim)
        ]
        for _ in range(payload["repetitions"]):
            for row in range(dim):
                for col in range(dim):
                    value = sum(left[row][k] * right[k][col] for k in range(dim)) & MASK
                    result = _rotate(result) ^ value
            left = [[(v + 1) & MASK for v in row] for row in left]
    else:
        values = [expected_output(key, value) for key, value in _mixed_parts(payload)]
        result = (
            _rotate(values[0])
            ^ _rotate(values[1], 13)
            ^ _rotate(values[2], 29)
            ^ values[3]
        )
    return result

"""Validate the exact, versioned schemas supplied by NanoFaaS."""

import json
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


def contract_asset(name: str) -> Traversable:
    """Resolve bundled data independently of the caller's checkout."""
    return files("nanolab").joinpath("assets", "one-shot", "contracts", name)


@lru_cache(maxsize=3)
def _validator(name: str) -> Draft202012Validator:
    if name not in {"service-profile", "forecast", "run-events"}:
        raise ValueError("unknown one-shot contract")
    schema = json.loads(contract_asset(f"{name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(name: str, data: object) -> None:
    """Reject malformed or incompatible protocol data at the API boundary."""
    try:
        _validator(name).validate(data)
    except ValidationError as error:
        raise ValueError(f"invalid {name}: {error.message}") from error

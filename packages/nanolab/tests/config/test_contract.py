from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig


def test_defaults() -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "contract",
            "backend": "container",
            "functions": ["word-stats"],
            "contract": {},
        }
    )
    assert config.contract is not None
    assert config.contract.capture_bytes == 33554432
    assert config.contract.build_seconds == 2700


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_seconds", 0),
        ("quiet_seconds", float("inf")),
        ("message_bytes", True),
        ("builder_cpu_quota", -1),
    ],
)
def test_invalid_budgets(field, value) -> None:
    with pytest.raises(ValidationError, match=field):
        ScenarioConfig.model_validate(
            {
                "workflow": "contract",
                "backend": "container",
                "functions": ["word-stats"],
                "contract": {field: value},
            }
        )


@pytest.mark.parametrize(
    "change",
    [
        {"backend": "k8s"},
        {"build": "buildpack"},
        {"asyncLoad": True},
        {"contract": None},
    ],
)
def test_contract_rejects_unused_options(change) -> None:
    with pytest.raises(ValidationError, match="contract"):
        ScenarioConfig.model_validate(
            {
                "workflow": "contract",
                "backend": "container",
                "functions": ["word-stats"],
                "contract": {},
                **change,
            }
        )


def test_contract_settings_rejected_elsewhere() -> None:
    with pytest.raises(ValidationError, match="contract"):
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "container",
                "functions": ["word-stats-java"],
                "contract": {},
            }
        )

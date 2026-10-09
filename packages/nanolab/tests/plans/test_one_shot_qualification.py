import hashlib

import pytest

from nanolab.config import ScenarioConfig
from nanolab.one_shot.contracts import contract_asset
from nanolab.plans.one_shot_qualification import build_one_shot_qualification_plan


def test_qualification_requires_explicit_periods_and_trace_resolution():
    from nanolab.config.one_shot import TimingSettings

    with pytest.raises(
        ValueError, match=r"periodCandidatesSeconds|maxTraceResolutionSeconds"
    ):
        TimingSettings.model_validate({})


def test_qualification_refuses_changed_profile_before_vm_acquisition(tmp_path):
    from nanolab.config import EnvironmentConfig

    profile = tmp_path / "profile.json"
    profile.write_bytes(contract_asset("examples/synthetic-profile.json").read_bytes())
    scenario = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-qualification",
            "backend": "container",
            "functions": ["one-shot-workload-rust"],
            "oneShot": {
                "provider": "multipass",
                "purpose": "workflow-validation",
                "nodes": [
                    {"id": "edge-0", "kind": "edge"},
                    {"id": "edge-1", "kind": "edge"},
                    {"id": "cloud", "kind": "cloud"},
                ],
                "functions": {"one-shot-workload-rust": {"input": {}}},
                "profile": {
                    "path": str(profile),
                    "sha256": hashlib.sha256(b"other").hexdigest(),
                },
                "timing": {
                    "periodCandidatesSeconds": [10, 20],
                    "maxTraceResolutionSeconds": 20,
                    "matrix": [
                        {
                            "id": "balanced",
                            "rates": {
                                "edge-0": {"one-shot-workload-rust": 4},
                                "edge-1": {"one-shot-workload-rust": 4},
                            },
                        }
                    ],
                },
            },
        }
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        build_one_shot_qualification_plan(
            scenario, environment, run_dir=tmp_path / "run", repo_root=tmp_path
        )
    assert not (tmp_path / "run").exists()

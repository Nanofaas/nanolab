import json

import pytest
from pydantic import ValidationError

from nanolab.workbench.inventory import HostVars, Instance, write_host_vars


def test_duplicate_instance_names_rejected():
    twice = Instance(name="a", component="loki")
    with pytest.raises(ValidationError, match="unique"):
        HostVars(instances=(twice, twice))


@pytest.mark.parametrize("cpuset", ["", "2-", "a", "3-1", "1,,2"])
def test_bad_cpuset_rejected(cpuset):
    with pytest.raises(ValidationError):
        Instance(name="a", component="loki", cpuset=cpuset)


def test_bad_name_and_relative_collect_dir_rejected():
    with pytest.raises(ValidationError):
        Instance(name="A_b", component="loki")
    with pytest.raises(ValidationError):
        HostVars(instances=(), collect_dir="relative/dir")


def test_vars_round_trip(tmp_path):
    host_vars = HostVars(
        instances=(
            Instance(name="loki", component="loki", cpuset="0,4-5", config={"port": 1}),
        ),
        collect_dir="/abs",
    )
    path = write_host_vars(tmp_path / "v.json", host_vars)
    assert HostVars.model_validate_json(path.read_text()) == host_vars
    assert json.loads(path.read_text())["instances"][0]["form"] == "container"

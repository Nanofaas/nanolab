"""Controlled CPU placement rejects ambiguous resource and hardware assignments."""

import copy
from pathlib import Path

import pytest

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.vm_resources import resource_scope


def configuration():
    return {
        "provider": "multipass",
        "purpose": "workflow-validation",
        "nodes": [
            {"id": "edge-0", "kind": "edge", "cpus": 1},
            {"id": "edge-1", "kind": "edge", "cpus": 1},
            {"id": "cloud", "kind": "cloud", "cpus": 2},
        ],
        "functions": {"f": {"input": {}, "cpu": 0.1}},
        "hostCpuPlacement": {
            "nodes": {
                "edge-0": {"cpus": [0], "maxFrequencyKHz": 2808000},
                "edge-1": {"cpus": [1], "maxFrequencyKHz": 2808000},
                "cloud": {"cpus": [2, 3], "maxFrequencyKHz": 3900000},
            },
            "operatorCpus": [4],
            "loggingCpus": [5, 6],
        },
    }


def test_cpu_policy_is_bound_to_resource_scope():
    first = configuration()
    second = copy.deepcopy(first)
    second["hostCpuPlacement"]["nodes"]["edge-0"]["cpus"] = [7]
    a, b = OneShotConfig.model_validate(first), OneShotConfig.model_validate(second)
    assert a.host_cpu_placement is not None
    assert a.host_cpu_placement.nodes["edge-0"].cpus == [0]
    assert resource_scope(a) != resource_scope(b)


@pytest.mark.parametrize("ids", [[True], [-1], [0, 0]])
def test_non_integer_negative_duplicate_cpu_ids_are_rejected(ids):
    data = configuration()
    data["hostCpuPlacement"]["operatorCpus"] = ids
    with pytest.raises(ValueError, match=r"integer|greater than|overlap"):
        OneShotConfig.model_validate(data)


def test_cpu_count_must_match_vm_vcpus():
    data = configuration()
    data["hostCpuPlacement"]["nodes"]["cloud"]["cpus"] = [2]
    with pytest.raises(ValueError, match="vCPU"):
        OneShotConfig.model_validate(data)


def test_cpu_node_coverage_must_be_exact():
    data = configuration()
    del data["hostCpuPlacement"]["nodes"]["edge-1"]
    with pytest.raises(ValueError, match="node"):
        OneShotConfig.model_validate(data)


@pytest.mark.parametrize("section", ["operatorCpus", "loggingCpus"])
def test_vm_and_host_reservations_do_not_overlap(section):
    data = configuration()
    data["hostCpuPlacement"][section] = [0, 6]
    with pytest.raises(ValueError, match="overlap"):
        OneShotConfig.model_validate(data)


def cpu_tree(root: Path):
    for cpu in range(8):
        directory = root / f"cpu{cpu}" / "cpufreq"
        directory.mkdir(parents=True)
        (directory / "cpuinfo_max_freq").write_text(
            "2808000" if cpu in (0, 1, 4, 7) else "3900000"
        )
        (directory / "scaling_governor").write_text("performance")
    return root


def test_hardware_family_and_governor_are_verified_before_acquisition(tmp_path):
    from nanolab.one_shot.host_cpu import validate_host_placement

    config = OneShotConfig.model_validate(configuration())
    assert config.host_cpu_placement is not None
    root = cpu_tree(tmp_path)
    facts = validate_host_placement(config.host_cpu_placement, cpu_root=root)
    assert facts["nodes"]["cloud"]["cpus"] == [2, 3]
    assert facts["nodes"]["cloud"]["maxFrequencyKHz"] == 3900000


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("family", "family"),
        ("governor", "governor"),
        ("offline", "online"),
        ("unavailable", "unavailable"),
    ],
)
def test_uncertain_or_changed_hardware_refuses_placement(tmp_path, change, message):
    from nanolab.one_shot.host_cpu import validate_host_placement

    policy = OneShotConfig.model_validate(configuration()).host_cpu_placement
    assert policy is not None
    root = cpu_tree(tmp_path)
    cpu = root / "cpu0"
    if change == "family":
        (cpu / "cpufreq/cpuinfo_max_freq").write_text("3900000")
    elif change == "governor":
        (cpu / "cpufreq/scaling_governor").write_text("schedutil")
    elif change == "offline":
        (cpu / "online").write_text("0")
    else:
        (cpu / "cpufreq/cpuinfo_max_freq").unlink()
    with pytest.raises(ValueError, match=message):
        validate_host_placement(policy, cpu_root=root)


def test_legacy_config_keeps_cpu_policy_optional():
    data = configuration()
    del data["hostCpuPlacement"]
    assert OneShotConfig.model_validate(data).host_cpu_placement is None


def qemu_process(root, pid, vm, threads=(101, 102)):
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / "comm").write_text("qemu-system-aar")
    (directory / "cmdline").write_bytes(
        f"qemu-system-aarch64\0-drive\0file=/vault/instances/{vm}/disk.qcow2,if=none\0".encode()
    )
    for tid in threads:
        (directory / "task" / str(tid)).mkdir(parents=True)
    return directory


def test_owned_qemu_match_uses_exact_vm_path_component(tmp_path):
    from nanolab.one_shot.host_cpu import owned_qemu_pid

    qemu_process(tmp_path, 101, "nl-os-test-edge-0")
    qemu_process(tmp_path, 201, "nl-os-test-edge-0-other")
    assert owned_qemu_pid("nl-os-test-edge-0", proc_root=tmp_path) == 101


def test_duplicate_vm_process_identity_is_not_safe_to_pin(tmp_path):
    from nanolab.one_shot.host_cpu import owned_qemu_pid

    qemu_process(tmp_path, 101, "nl-os-test-edge-0")
    qemu_process(tmp_path, 201, "nl-os-test-edge-0")
    with pytest.raises(ValueError, match="exactly one"):
        owned_qemu_pid("nl-os-test-edge-0", proc_root=tmp_path)


def test_no_matching_vm_process_is_not_an_empty_affinity_success(tmp_path):
    from nanolab.one_shot.host_cpu import owned_qemu_pid

    qemu_process(tmp_path, 201, "foreign")
    with pytest.raises(ValueError, match="exactly one"):
        owned_qemu_pid("nl-os-test-edge-0", proc_root=tmp_path)


def test_every_thread_must_match_the_frozen_mask(tmp_path, monkeypatch):
    from nanolab.one_shot.host_cpu import inspect_vm_placement

    qemu_process(tmp_path, 101, "nl-os-test-edge-0")
    monkeypatch.setattr(
        "os.sched_getaffinity", lambda tid: {0} if tid == 101 else {0, 1}
    )
    with pytest.raises(ValueError, match="affinity"):
        inspect_vm_placement("nl-os-test-edge-0", [0], proc_root=tmp_path)


def test_missing_thread_evidence_refuses_placement(tmp_path):
    from nanolab.one_shot.host_cpu import inspect_vm_placement

    qemu_process(tmp_path, 101, "nl-os-test-edge-0", threads=())
    with pytest.raises(ValueError, match="thread"):
        inspect_vm_placement("nl-os-test-edge-0", [0], proc_root=tmp_path)


def test_pin_vm_only_mutates_the_verified_owned_process(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nanolab.one_shot.host_cpu import pin_vm

    qemu_process(tmp_path, 101, "nl-os-test-edge-0")
    qemu_process(tmp_path, 201, "foreign", threads=(201,))
    masks = {101: {0, 1}, 102: {0, 1}, 201: {0, 1}}
    monkeypatch.setattr("os.sched_getaffinity", lambda tid: masks[tid])

    def taskset(argv, **kwargs):
        assert argv == ["sudo", "-n", "taskset", "-apc", "0", "101"]
        masks[101] = masks[102] = {0}
        return SimpleNamespace(stdout="owned threads pinned", stderr="", returncode=0)

    monkeypatch.setattr("nanolab.one_shot.host_cpu.subprocess.run", taskset)
    result = pin_vm("nl-os-test-edge-0", [0], proc_root=tmp_path)
    assert result["qemuPid"] == 101
    assert result["threadMasks"] == {"101": [0], "102": [0]}
    assert masks[201] == {0, 1}


def test_operator_restores_existing_and_new_threads(tmp_path, monkeypatch):
    from nanolab.one_shot.host_cpu import pin_operator, restore_operator

    tasks = tmp_path / "self/task"
    for tid in (101, 102):
        (tasks / str(tid)).mkdir(parents=True)
    masks = {101: {0, 1}, 102: {2, 3}}
    monkeypatch.setattr("os.getpid", lambda: 101)
    monkeypatch.setattr("os.sched_getaffinity", lambda tid: masks[tid])
    monkeypatch.setattr(
        "os.sched_setaffinity", lambda tid, cpus: masks.__setitem__(tid, set(cpus))
    )
    original = pin_operator([4], proc_root=tmp_path)
    assert masks == {101: {4}, 102: {4}}
    (tasks / "103").mkdir()
    masks[103] = {4}
    restore_operator(original, proc_root=tmp_path)
    assert masks == {101: {0, 1}, 102: {2, 3}, 103: {0, 1}}


def test_partial_operator_pin_failure_restores_original_masks(tmp_path, monkeypatch):
    from nanolab.one_shot.host_cpu import pin_operator

    for tid in (101, 102):
        (tmp_path / "self/task" / str(tid)).mkdir(parents=True)
    masks = {101: {0, 1}, 102: {2, 3}}
    monkeypatch.setattr("os.getpid", lambda: 101)
    monkeypatch.setattr("os.sched_getaffinity", lambda tid: masks[tid])

    def set_mask(tid, cpus):
        if tid == 102 and set(cpus) == {4}:
            raise OSError("operator pin refused")
        masks[tid] = set(cpus)

    monkeypatch.setattr("os.sched_setaffinity", set_mask)
    with pytest.raises(OSError, match="pin refused"):
        pin_operator([4], proc_root=tmp_path)
    assert masks == {101: {0, 1}, 102: {2, 3}}


def test_failed_vm_placement_releases_owned_vm_and_operator(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sonata_engine import Task, TaskOutcome, Workflow

    from nanolab.config import EnvironmentConfig
    from nanolab.one_shot.infrastructure import build_one_shot_resources

    events = []
    acquired, released = [], []

    class Provider:
        def vm_exists(self, request):
            return False

        def ensure_running(self, request):
            acquired.append(request.name)
            events.append("acquire-vm")
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        def connection_host(self, request):
            return "10.0.0.1"

        def teardown(self, request):
            released.append(request.name)
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    def pin(name, cpus):
        if len(acquired) == 2:
            raise RuntimeError("VM pin refused")
        return {"cpus": cpus}

    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.provider_for", lambda *_: Provider()
    )
    monkeypatch.setattr(
        "nanolab.one_shot.host_cpu.validate_host_placement", lambda *_: {}
    )
    monkeypatch.setattr("nanolab.one_shot.host_cpu.pin_vm", pin)
    monkeypatch.setattr(
        "nanolab.one_shot.host_cpu.pin_operator",
        lambda *_: events.append("pin-operator") or {},
    )
    monkeypatch.setattr(
        "nanolab.one_shot.host_cpu.restore_operator",
        lambda *_: events.append("restore-operator"),
    )
    config = OneShotConfig.model_validate(configuration())
    env = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    resources = build_one_shot_resources(config, env, run_id="test", repo_root=tmp_path)

    class Observe(Task):
        title = "Observe placement"

        def run(self, inputs):
            return TaskOutcome(value=None)

    workflow = Workflow("placement-failure")
    workflow.add(Observe(), requires=resources.vms)
    with pytest.raises(RuntimeError, match="VM pin refused"):
        workflow.run()
    assert events[0] == "pin-operator"
    assert released == list(reversed(acquired))
    assert events[-1] == "restore-operator"


@pytest.mark.parametrize("foreign", [False, True])
def test_logging_project_cpu_pin_checks_ownership_before_update(monkeypatch, foreign):
    import json
    from types import SimpleNamespace

    from nanolab.one_shot.host_cpu import pin_logging_project

    project = "nanolab-logs-aabbccddeeff"
    masks = {"loki-id": "", "grafana-id": ""}
    updated = []

    def docker(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                stdout="loki-id\ngrafana-id\n", stderr="", returncode=0
            )
        if argv[:2] == ["docker", "inspect"]:
            body = [
                {
                    "id": key,
                    "project": "foreign" if foreign else project,
                    "cpuset": mask,
                }
                for key, mask in masks.items()
            ]
            return SimpleNamespace(
                stdout="\n".join(json.dumps(row) for row in body),
                stderr="",
                returncode=0,
            )
        assert argv[:2] == ["docker", "update"]
        updated.extend(argv[4:])
        for key in argv[4:]:
            masks[key] = argv[3]
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("nanolab.one_shot.host_cpu.subprocess.run", docker)
    if foreign:
        with pytest.raises(ValueError, match="ownership"):
            pin_logging_project(project, [5, 6])
        assert updated == []
    else:
        facts = pin_logging_project(project, [5, 6])
        assert set(updated) == set(masks)
        assert all(row["cpuset"] == "5,6" for row in facts)


@pytest.mark.parametrize("drift", [False, True])
def test_preflight_rechecks_vm_and_operator_placement(tmp_path, monkeypatch, drift):
    from nanolab.one_shot.host_cpu import collect_host_placement

    policy = OneShotConfig.model_validate(configuration()).host_cpu_placement
    assert policy is not None
    cpu_root = cpu_tree(tmp_path / "sys")
    proc_root = tmp_path / "proc"
    names = {node: "nl-os-test-" + node for node in policy.nodes}
    masks = {}
    for index, (node, name) in enumerate(names.items(), start=1):
        pid = index * 100
        qemu_process(proc_root, pid, name, threads=(pid, pid + 1))
        masks[pid] = masks[pid + 1] = set(policy.nodes[node].cpus)
    (proc_root / "self/task/401").mkdir(parents=True)
    masks[401] = {4}
    if drift:
        masks[101] = {0, 1}
    monkeypatch.setattr("os.sched_getaffinity", lambda tid: masks[tid])
    if drift:
        with pytest.raises(ValueError, match="affinity"):
            collect_host_placement(
                policy, names, proc_root=proc_root, cpu_root=cpu_root
            )
    else:
        facts = collect_host_placement(
            policy, names, proc_root=proc_root, cpu_root=cpu_root
        )
        assert facts["nodes"]["edge-0"]["cpus"] == [0]
        assert facts["nodes"]["cloud"]["cpus"] == [2, 3]
        assert facts["operatorThreadMasks"] == {"401": [4]}

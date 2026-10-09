from nanolab.tasks.one_shot.conservation import evaluate_conservation


def rows(n):
    return [
        {"originalId": str(i), "origin": "edge-0", "function": "work", "epoch": 0}
        for i in range(n)
    ]


def completed(rows):
    return [
        {
            **row,
            "state": "RELEASED",
            "handlerStarted": True,
            "responseStatus": "SUCCESS",
            "destination": "edge-0",
        }
        for row in rows
    ]


def results(rows):
    return [{**row, "success": True} for row in rows]


def test_generator_deficit_is_explicit():
    planned, emitted = rows(100), rows(90)
    evidence = evaluate_conservation(
        planned, emitted, completed(emitted), results(emitted)
    )
    assert evidence.planned == 100 and evidence.emitted == 90
    assert evidence.generator_deficit == 10 and not evidence.valid


def test_no_load_never_passes_zero_equals_zero():
    assert not evaluate_conservation([], [], [], []).valid
    assert not evaluate_conservation(rows(10), [], [], []).valid


def test_duplicate_attempts_do_not_inflate_original_completions():
    planned = rows(1)
    evidence = evaluate_conservation(
        planned, planned, completed(planned) * 2, results(planned)
    )
    assert evidence.completed == 1 and evidence.attempts == 2
    assert not evidence.valid
    assert evaluate_conservation(
        planned, planned, completed(planned), results(planned)
    ).valid


def test_repeated_collection_deduplicates_proof_observations_not_attempts(tmp_path):
    import json

    from nanolab.tasks.one_shot.experiment import physical_attempts

    proof = {
        **completed(rows(1))[0],
        "executionId": "x",
        "incarnation": "i",
        "dispatchAttempt": "1",
    }
    path = tmp_path / "physical.jsonl"
    path.write_text(json.dumps(proof) + "\n" + json.dumps(proof) + "\n")
    assert len(physical_attempts(path)) == 1
    path.write_text(
        path.read_text() + json.dumps({**proof, "dispatchAttempt": "2"}) + "\n"
    )
    assert len(physical_attempts(path)) == 2


def test_plan_verification_rejects_ram_and_capacity_violations():
    import pytest

    from nanolab.tasks.one_shot.experiment import verify_ready_plan

    good = {
        "functions": {
            "work": {
                "readyReplicas": 1,
                "memoryMiB": 128,
                "localRate": 4,
                "demandSeconds": 0.1,
                "utilization": 0.8,
                "inbound": [],
            }
        },
        "flowQuantum": 1,
    }
    verify_ready_plan(good, memory_capacity=128)
    with pytest.raises(ValueError, match="RAM"):
        verify_ready_plan(good, memory_capacity=64)
    good["functions"]["work"]["localRate"] = 9
    with pytest.raises(ValueError, match="capacity"):
        verify_ready_plan(good, memory_capacity=128)


def test_terminal_invocation_errors_are_accounted_without_fake_physical_success():
    planned = rows(2)
    result = [
        {**planned[0], "success": True},
        {**planned[1], "success": False, "terminalError": True},
    ]
    evidence = evaluate_conservation(planned, planned, completed(planned[:1]), result)
    assert evidence.valid
    assert evidence.completed == 1 and evidence.errors == 1 and evidence.received == 1
    result[1]["terminalError"] = False
    assert not evaluate_conservation(
        planned, planned, completed(planned[:1]), result
    ).valid


def test_runtime_success_status_uses_the_real_lowercase_wire_value():
    planned = rows(1)
    proof = completed(planned)
    proof[0]["responseStatus"] = "success"
    assert (
        evaluate_conservation(planned, planned, proof, results(planned)).completed == 1
    )


def test_unplanned_terminal_error_is_not_silently_accepted():
    planned = rows(1)
    result = [*results(planned), {"originalId": "unknown", "terminalError": True}]
    assert not evaluate_conservation(planned, planned, completed(planned), result).valid

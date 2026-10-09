from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def resolve(source: Path, selectors=("word-stats",)):
    from nanolab.functions.contracts import resolve_contract_matrix

    return resolve_contract_matrix(
        source, selectors, architecture="arm64", tag="attempt"
    )


@pytest.fixture
def frozen_fixture(tmp_path):
    for name in (
        "platform/control-plane",
        "services/java/warm-echo",
        "runtimes/watchdog",
        "tools/native-java",
        "functions/python/word-stats",
    ):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "build.gradle").write_text("version = '1.0.0'\n")
    corpus = tmp_path / "functions/test-data/word-stats/correctness.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text(
        json.dumps({"cases": [{"name": "one", "input": {}, "expected": {"answer": 1}}]})
    )
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "add", "."), cwd=tmp_path, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ),
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def corpus(source):
    return source / "functions/test-data/word-stats/correctness.json"


def test_empty_selection(frozen_fixture):
    with pytest.raises(ValueError, match="empty"):
        resolve(frozen_fixture, ())


def test_duplicate_json_keys_rejected(frozen_fixture):
    corpus(frozen_fixture).write_text('{"cases": [], "cases": []}')
    with pytest.raises(ValueError, match="duplicate"):
        resolve(frozen_fixture)


def test_boolean_status_rejected(frozen_fixture):
    corpus(frozen_fixture).write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "one",
                        "input": {},
                        "expected": {},
                        "expectedStatusCode": True,
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="status"):
        resolve(frozen_fixture)


def test_nonfinite_json_rejected(frozen_fixture):
    corpus(frozen_fixture).write_text(
        '{"cases":[{"name":"one","input":NaN,"expected":{}}]}'
    )
    with pytest.raises(ValueError, match="finite"):
        resolve(frozen_fixture)


@pytest.mark.parametrize(
    "cases",
    [
        [{"name": "missing", "input": {}}],
        [
            {"name": "one", "input": {}, "expected": 1},
            {"name": "two", "input": {}, "expected": 2},
        ],
    ],
)
def test_missing_and_ambiguous_oracles_rejected(frozen_fixture, cases):
    corpus(frozen_fixture).write_text(json.dumps({"cases": cases}))
    with pytest.raises(ValueError, match=r"oracle|ambiguous"):
        resolve(frozen_fixture)


@pytest.mark.parametrize("selector", ["unknown", "word-stats-rust"])
def test_unknown_and_missing_selectors_rejected(frozen_fixture, selector):
    with pytest.raises(ValueError, match="selector"):
        resolve(frozen_fixture, (selector,))


def test_payload_expected_cannot_override_oracle(frozen_fixture):
    payload = frozen_fixture / "functions/python/word-stats/payloads/one.json"
    payload.parent.mkdir()
    payload.write_text(json.dumps({"input": {}, "expected": {"answer": 2}}))
    with pytest.raises(ValueError, match="oracle"):
        resolve(frozen_fixture)


def test_catalog_addition_changes_matrix(frozen_fixture):
    first = resolve(frozen_fixture)
    assert [cell.target.name for cell in first.images.cells] == ["python-word-stats"]
    added = frozen_fixture / "functions/go/word-stats"
    added.mkdir(parents=True)
    (added / "Dockerfile").write_text("FROM scratch\n")
    second = resolve(frozen_fixture)
    assert {cell.target.name for cell in second.images.cells} == {
        "go-word-stats",
        "python-word-stats",
    }


def test_rust_catalog_addition_joins_family_and_explicit_contracts(frozen_fixture):
    added = frozen_fixture / "functions/rust/word-stats"
    added.mkdir(parents=True)
    (added / "Dockerfile").write_text("FROM scratch\n")
    family = resolve(frozen_fixture)
    assert {cell.target.name for cell in family.images.cells} == {
        "rust-word-stats",
        "python-word-stats",
    }
    selected = resolve(frozen_fixture, ("word-stats-rust",))
    assert [cell.target.name for cell in selected.images.cells] == ["rust-word-stats"]


@pytest.mark.parametrize("target", ["corpus", "payload"])
def test_escaped_corpus_or_payload_rejected(frozen_fixture, tmp_path, target):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text(corpus(frozen_fixture).read_text())
    path = (
        corpus(frozen_fixture)
        if target == "corpus"
        else frozen_fixture / "functions/python/word-stats/payloads/escaped.json"
    )
    path.parent.mkdir(exist_ok=True)
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="source"):
        resolve(frozen_fixture)


@pytest.mark.nanofaas
def test_current_catalog_matrix(nanofaas_checkout):
    from nanolab.functions.contracts import resolve_contract_matrix

    matrix = resolve_contract_matrix(
        nanofaas_checkout,
        ("word-stats", "json-transform", "roman-numeral", "qr-code"),
        architecture="arm64",
        tag="attempt",
    )
    assert len(matrix.images.cells) == 31
    counts = {"word-stats": 3, "json-transform": 3, "roman-numeral": 5, "qr-code": 7}
    assert {family: len(cases) for family, cases in matrix.cases.items()} == counts
    assert (
        sum(
            counts[cell.target.name.split("-", 1)[1].removeprefix("lite-")]
            for cell in matrix.images.cells
        )
        == 137
    )


def test_nonfinite_exponent_rejected(frozen_fixture):
    corpus(frozen_fixture).write_text(
        '{"cases":[{"name":"one","input":1e10000,"expected":{}}]}'
    )
    with pytest.raises(ValueError, match="finite"):
        resolve(frozen_fixture)

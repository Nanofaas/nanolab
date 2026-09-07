"""The load generator's payload comes from the repository-owned corpora."""
from __future__ import annotations

from pathlib import Path

import pytest

from nanolab.config import ScenarioConfig
from nanolab.plans.loadtest import payload_corpus_path


def _config(**overrides) -> ScenarioConfig:
    base = dict(workflow="loadtest", backend="container", concurrencyControl=True,
                functions=["word-stats-java"])
    base.update(overrides)
    return ScenarioConfig(**base)


def test_no_profile_means_the_generator_keeps_its_built_in_text(tmp_path: Path) -> None:
    assert payload_corpus_path(_config(), tmp_path, "word-stats-java") is None


def test_a_profile_resolves_to_the_family_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "functions" / "test-data" / "word-stats" / "performance-medium.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text("{}")

    resolved = payload_corpus_path(_config(payloadProfile="medium"), tmp_path, "word-stats-java")

    assert resolved == corpus


def test_the_family_is_shared_across_runtimes(tmp_path: Path) -> None:
    """word-stats-java and word-stats-java-lite are one family, and one corpus.

    That is the point of the corpora: the same bytes replayed against different
    runtimes, so a difference between them is the runtime and not the input.
    """
    corpus = tmp_path / "functions" / "test-data" / "word-stats" / "performance-large.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text("{}")

    for function in ("word-stats-java", "word-stats-java-lite"):
        assert payload_corpus_path(
            _config(functions=[function], payloadProfile="large"), tmp_path, function
        ) == corpus


def test_a_missing_corpus_fails_loudly(tmp_path: Path) -> None:
    """Silently falling back would leave the run measuring an idle function.

    That is exactly the failure this knob exists to prevent, so it must not be
    the failure mode of the knob itself.
    """
    with pytest.raises(FileNotFoundError, match="performance-medium.json"):
        payload_corpus_path(_config(payloadProfile="medium"), tmp_path, "word-stats-java")

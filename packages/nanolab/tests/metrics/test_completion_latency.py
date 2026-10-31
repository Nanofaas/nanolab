"""A3 must retain distributions for both functions and fail before load if absent."""

from nanolab.config.scenario import ScenarioConfig
from nanolab.metrics.catalogue import queries_for


def test_completion_collection_is_explicit_and_preserves_histogram_labels():
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "loadProfile": "mixed",
            "functions": ["word-stats-java", "word-stats-javascript"],
            "completionLatency": True,
        }
    )
    assert config.completion_latency
    queries = queries_for(
        "word-stats-java",
        modules=("async-queue",),
        neighbour="word-stats-javascript",
        completion_latency=True,
    )
    selected = [q for q in queries if q.name.startswith("function_completion_latency_")]
    assert len(selected) == 6
    assert all(q.required for q in selected)
    for query in selected:
        assert "sum(" not in query.expr and "rate(" not in query.expr
        assert "path=" not in query.expr and "outcome=" not in query.expr
    assert any(
        "_bucket{" in q.expr and "word-stats-javascript" in q.expr for q in selected
    )
    assert not any(
        q.name.startswith("function_completion_latency_")
        for q in queries_for("word-stats-java", modules=())
    )

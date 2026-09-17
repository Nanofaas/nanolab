from types import SimpleNamespace

import pytest
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.plans.heap_analysis import (
    build_heap_analysis_plan,
    unique_heap_analysis_run_dir,
)


class _CompileOnlyExecutor:
    def binding_key(self, role):
        return f"compile-only:{role}"

    def run(self, task, *, dry_run=False):
        raise AssertionError("plan construction must not execute commands")


def test_constructor_compiles_deferred_pipeline_without_side_effects(tmp_path):
    workflow = build_heap_analysis_plan(
        SimpleNamespace(workflow="heap-analysis", heap_analysis=object()),  # pyright: ignore[reportArgumentType]
        SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
        RoleBindings({"host": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )
    titles = [task.task.title for task in workflow.compile().tasks]
    measure = next(
        i for i, title in enumerate(titles) if "Capture and analyze" in title
    )
    # Acquired before the run and released after it: preparation pushes the
    # application images and the helper build pushes the helper, neither of which
    # has happened yet when the workflow is compiled and both of which happen once
    # the measuring task starts.
    assert any("Acquire local registry" in title for title in titles[:measure])
    assert any("Release local registry" in title for title in titles[measure + 1 :])
    assert not (tmp_path / "run").exists()


def test_plan_rejects_non_heap_analysis_scenario(tmp_path):
    with pytest.raises(ValueError, match="requires a heap-analysis scenario"):
        build_heap_analysis_plan(
            SimpleNamespace(workflow="soak", heap_analysis=None),  # pyright: ignore[reportArgumentType]
            SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
            RoleBindings({"host": _CompileOnlyExecutor()}),
            run_dir=tmp_path / "run",
            repo_root=tmp_path,
            tool_root=tmp_path,
        )


def test_plan_rejects_missing_heap_analysis_block(tmp_path):
    with pytest.raises(ValueError, match="requires a heap-analysis scenario"):
        build_heap_analysis_plan(
            SimpleNamespace(workflow="heap-analysis", heap_analysis=None),  # pyright: ignore[reportArgumentType]
            SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
            RoleBindings({"host": _CompileOnlyExecutor()}),
            run_dir=tmp_path / "run",
            repo_root=tmp_path,
            tool_root=tmp_path,
        )


def test_plan_rejects_non_local_environment(tmp_path):
    with pytest.raises(ValueError, match="requires a local environment"):
        build_heap_analysis_plan(
            SimpleNamespace(workflow="heap-analysis", heap_analysis=object()),  # pyright: ignore[reportArgumentType]
            SimpleNamespace(provider="azure"),  # pyright: ignore[reportArgumentType]
            RoleBindings({"host": _CompileOnlyExecutor()}),
            run_dir=tmp_path / "run",
            repo_root=tmp_path,
            tool_root=tmp_path,
        )


def test_heap_analysis_run_directories_are_unique(tmp_path):
    assert unique_heap_analysis_run_dir(tmp_path) != unique_heap_analysis_run_dir(
        tmp_path
    )
    assert unique_heap_analysis_run_dir(tmp_path).name.startswith("heap-analysis-")

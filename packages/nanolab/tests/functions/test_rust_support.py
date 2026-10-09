from pathlib import Path

import pytest

from nanolab.application.functions import resolve_function, sonata_function
from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.catalog import list_functions
from nanolab.images.plan import build_image_plan

FAMILIES = ("word-stats", "json-transform", "roman-numeral", "qr-code")


@pytest.fixture
def rust_source(tmp_path: Path) -> Path:
    for directory in (
        "platform/control-plane",
        "services/java/warm-echo",
        "runtimes/watchdog",
        "tools/native-java",
    ):
        path = tmp_path / directory
        path.mkdir(parents=True)
        (path / "Dockerfile").write_text("FROM scratch\n")
    for family in FAMILIES:
        path = tmp_path / "functions/rust" / family
        path.mkdir(parents=True)
        (path / "Dockerfile").write_text("FROM scratch\n")
        (path / "function.yaml").write_text(
            f"name: {family}-rust\ncatalog:\n  runtime: rust\n"
            f"  family: {family}\n"
            f"  defaultImage: 127.0.0.1:5000/nanofaas/rust-{family}:e2e\n"
        )
    return tmp_path


def test_catalog_discovers_all_four_rust_families(rust_source: Path) -> None:
    assert {f.key for f in list_functions(rust_source) if f.runtime == "rust"} == {
        "word-stats-rust",
        "json-transform-rust",
        "roman-numeral-rust",
        "qr-code-rust",
    }


@pytest.mark.parametrize("family", FAMILIES)
def test_rust_build_and_deployment_use_the_root_context(
    rust_source: Path, family: str
) -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "container",
            "functions": [f"{family}-rust"],
        }
    )
    function = resolve_function(config, f"{family}-rust", source_root=rust_source)
    assert function.build_argv == (
        "docker",
        "build",
        "-t",
        f"127.0.0.1:5000/nanofaas/rust-{family}:e2e",
        "-f",
        f"functions/rust/{family}/Dockerfile",
        ".",
    )
    assert function.image_build_argv is None
    deployment = sonata_function(function)
    assert deployment.name == f"{family}-rust"
    assert deployment.image == f"127.0.0.1:5000/nanofaas/rust-{family}:e2e"


def test_rust_images_use_default_flavor_on_both_architectures(
    rust_source: Path,
) -> None:
    plan = build_image_plan(
        rust_source,
        "v1.0.0",
        selectors=(
            "rust-word-stats",
            "rust-json-transform",
            "rust-roman-numeral",
            "rust-qr-code",
        ),
    )
    assert len(plan.cells) == 8
    assert {c.target.name for c in plan.cells} == {
        "rust-word-stats",
        "rust-json-transform",
        "rust-roman-numeral",
        "rust-qr-code",
    }
    assert {c.architecture for c in plan.cells} == {"amd64", "arm64"}
    for cell in plan.cells:
        assert cell.flavor == "default"
        assert cell.context == Path()
        assert (
            cell.dockerfile
            == Path("functions/rust")
            / cell.target.name.removeprefix("rust-")
            / "Dockerfile"
        )
        assert cell.prerequisite_command is None

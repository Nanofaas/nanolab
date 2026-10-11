import pytest

from tests.conftest import source_contract_root

pytestmark = pytest.mark.nanofaas

NANOFAAS_ROOT = source_contract_root()


def test_javascript_example_images_copy_local_sdk_dependency_target() -> None:
    dockerfiles = (
        NANOFAAS_ROOT / "functions" / "javascript" / "word-stats" / "Dockerfile",
        NANOFAAS_ROOT / "functions" / "javascript" / "json-transform" / "Dockerfile",
        NANOFAAS_ROOT / "functions" / "javascript" / "roman-numeral" / "Dockerfile",
    )

    for dockerfile in dockerfiles:
        text = dockerfile.read_text(encoding="utf-8")
        if "COPY --from=build /src/sdks/javascript /sdks/javascript" in text:
            continue
        for runtime_path in ("package.json", "dist", "node_modules"):
            assert (
                f"COPY --from=build /src/sdks/javascript/{runtime_path} "
                f"/sdks/javascript/{runtime_path}" in text
            ), (
                f"{dockerfile} must supply SDK {runtime_path} "
                "at the dependency symlink target."
            )

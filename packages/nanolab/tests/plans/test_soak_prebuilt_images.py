"""Prebuilt soak images reach the plan without silently rebuilding a runtime."""

import pytest

from nanolab.plans.loadtest import _build_platform_request
from nanolab.tasks.platform import PlatformFunction
from nanolab.workspace.paths import default_tool_paths


@pytest.mark.parametrize(
    ("backend", "prebuilt", "build_images", "push_images"),
    [
        ("container", True, False, True),
        ("container", False, True, True),
        ("k8s", True, False, False),
    ],
)
def test_prebuilt_container_images_are_published_without_rebuilding(
    backend,
    prebuilt,
    build_images,
    push_images,
) -> None:
    function_image = "127.0.0.1:5000/nanofaas/java-word-stats:native"
    request = _build_platform_request(
        backend=backend,
        build="docker",
        functions=(
            PlatformFunction(
                name="word-stats-java",
                image=function_image,
                payload="{}",
                build_argv=("must-not-build-prebuilt-image",),
            ),
        ),
        additional_modules=(),
        functions_prebuilt=prebuilt,
        prebuilt_control_plane_image="nanofaas/control-plane:native",
        root=default_tool_paths().nanofaas_root,
        remote_repo_root=None,
        hpa=False,
    )

    assert request.build_images is build_images
    assert request.push_function_images is push_images
    assert request.functions[0].image == function_image

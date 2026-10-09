"""Keep the physical SDK release proof in the shared registration path."""

from nanolab.tasks.platform import PlatformFunction


def test_platform_function_preserves_runtime_environment():
    function = PlatformFunction(
        name="one-shot-workload",
        image="sha256:" + "a" * 64,
        payload="{}",
        build_argv=(),
        env={
            "NANOFAAS_ONE_SHOT_PROFILE": "true",
            "NANOFAAS_MAX_CONCURRENT_HANDLERS": "1",
        },
    )
    assert function.manifest().body()["env"] == function.env

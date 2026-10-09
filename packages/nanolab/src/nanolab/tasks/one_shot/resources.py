"""Shared node resource interfaces for the one-shot Sonata tasks."""

from nanolab.one_shot.infrastructure import (
    NodeExecutor,
    NodeResource,
    OneShotResources,
    build_one_shot_resources,
)

__all__ = [
    "NodeExecutor",
    "NodeResource",
    "OneShotResources",
    "build_one_shot_resources",
]


def node_probe_resource(node: NodeResource):
    """Own a persistent clock/network probe until the last topology consumer."""
    import json
    import time
    from importlib.resources import as_file, files

    import httpx
    from sonata_engine import TaskInputs
    from sonata_tasks.compensation import compensated_resource

    pid: int | None = None

    def stop(_inputs: TaskInputs, _url: str | None = None) -> None:
        if pid is not None:
            node.command(("kill", str(pid)))

    def acquire(inputs: TaskInputs) -> str:
        nonlocal pid
        vm = inputs.resource(node.vm)
        home = vm.home
        script = f"{home}/one-shot-probe.py"
        with as_file(
            files("nanolab").joinpath("assets", "one-shot", "probe.py")
        ) as source:
            result = node.provider.transfer_to(
                node.request, source=source, destination=script
            )
        if result.return_code != 0:
            raise RuntimeError("failed to stage one-shot clock probe")
        code = (
            "import subprocess; "
            f"f=open({json.dumps(home + '/one-shot-probe.log')},'wb'); "
            f"p=subprocess.Popen(['python3',{json.dumps(script)}],"
            "stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True); "
            "print(p.pid)"
        )
        pid = int(node.command(("python3", "-c", code)).stdout.strip())
        url = f"http://{vm.host}:17111"
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    http.get(url + "/inventory", timeout=1).raise_for_status()
                    return url
                except httpx.HTTPError:
                    time.sleep(0.1)
        raise RuntimeError("clock probe readiness timed out")

    return compensated_resource(
        title=f"Acquire {node.config.id} clock/network probe",
        acquire=acquire,
        release=stop,
        compensate=stop,
        requires=(node.vm,),
    )

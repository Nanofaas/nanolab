"""Capture B4 evidence on its publisher VM, without exposing management over WAN."""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def capture(namespace, start=None):
    result = {"captured_at": time.time(), "errors": []}
    try:
        result["inventory"] = json.loads(subprocess.check_output(
            ["kubectl", "get", "pods,deployments", "-n", namespace, "-o", "json"],
            timeout=30))
    except Exception as error:
        result["errors"].append(f"inventory: {error}")
    try:
        with urllib.request.urlopen("http://127.0.0.1:30081/actuator/prometheus", timeout=15) as response:
            result["metrics"] = response.read().decode()
    except Exception as error:
        result["errors"].append(f"metrics: {error}")
    if start is not None:
        query = '{__name__=~"function_.*|nanofaas_offload.*|process_uptime_seconds|container_memory_working_set_bytes|container_memory_rss|container_cpu_usage_seconds_total|up"}'
        parameters = urllib.parse.urlencode({"query": query, "start": start, "end": time.time(), "step": 5})
        try:
            with urllib.request.urlopen("http://127.0.0.1:30090/api/v1/query_range?" + parameters, timeout=60) as response:
                result["history"] = json.load(response)
            if result["history"].get("status") != "success":
                result["errors"].append("history: Prometheus query failed")
        except Exception as error:
            result["errors"].append(f"history: {error}")
    return result


if __name__ == "__main__":
    print(json.dumps(capture(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else None)))

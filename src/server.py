from typing import Any, Dict, List, Optional

from kubernetes import client, config
from kubernetes.client import ApiException
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("eks-pod-doctor")
_KUBE_LOADED = False


def _load_kube_config() -> None:
    global _KUBE_LOADED
    if _KUBE_LOADED:
        return

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

    _KUBE_LOADED = True


def _safe_message(error: Exception) -> str:
    if isinstance(error, ApiException):
        return f"kubernetes api error ({error.status}): {error.reason}"
    return str(error)


def _collect_pod_findings(pod: client.V1Pod, events: List[client.CoreV1Event]) -> List[str]:
    findings: List[str] = []
    statuses = pod.status.container_statuses or []
    init_statuses = pod.status.init_container_statuses or []

    for status in init_statuses:
        waiting = status.state.waiting if status.state else None
        if waiting:
            findings.append(
                f"init container '{status.name}' is waiting: {waiting.reason or 'Unknown'} - {waiting.message or 'no details'}"
            )

    for status in statuses:
        waiting = status.state.waiting if status.state else None
        terminated = status.last_state.terminated if status.last_state else None

        if waiting:
            reason = waiting.reason or "Unknown"
            if reason in {"ImagePullBackOff", "ErrImagePull"}:
                findings.append(
                    f"container '{status.name}' has {reason}; check image name/tag, registry access, and imagePullSecrets"
                )
            elif reason == "CrashLoopBackOff":
                findings.append(
                    f"container '{status.name}' is in CrashLoopBackOff; inspect previous logs for stack traces or startup failures"
                )
            elif reason == "CreateContainerConfigError":
                findings.append(
                    f"container '{status.name}' has CreateContainerConfigError; verify env refs, ConfigMaps, and Secrets"
                )
            elif reason == "RunContainerError":
                findings.append(
                    f"container '{status.name}' has RunContainerError; verify command/entrypoint and filesystem paths"
                )
            elif reason == "ContainerCreating":
                findings.append(
                    f"container '{status.name}' is still creating; check events for volume mounts or CNI delays"
                )
            else:
                findings.append(f"container '{status.name}' waiting reason: {reason}")

        if terminated:
            term_reason = terminated.reason or "Unknown"
            if term_reason == "OOMKilled":
                findings.append(
                    f"container '{status.name}' was OOMKilled; increase memory requests/limits or reduce memory usage"
                )
            elif terminated.exit_code not in (0, None):
                findings.append(
                    f"container '{status.name}' terminated with exit code {terminated.exit_code} ({term_reason})"
                )

    for condition in pod.status.conditions or []:
        if condition.type == "PodScheduled" and condition.status == "False":
            findings.append(f"pod scheduling failed: {condition.reason or 'Unknown'} - {condition.message or 'no details'}")
        if condition.type == "Ready" and condition.status == "False":
            findings.append(f"pod is not ready: {condition.reason or 'Unknown'} - {condition.message or 'no details'}")

    warning_events = [e for e in events if (e.type or "").lower() == "warning"]
    for event in warning_events[:5]:
        findings.append(
            f"warning event ({event.reason or 'Unknown'}): {event.message or 'no details'}"
        )

    if pod.status.phase == "Pending" and not findings:
        findings.append(
            "pod is Pending without a direct container error; likely scheduling or resource constraints"
        )

    if not findings:
        findings.append("no obvious issue detected from status/events; inspect app logs and dependencies")

    return findings


def _core_api() -> client.CoreV1Api:
    _load_kube_config()
    return client.CoreV1Api()


@mcp.tool()
def list_namespaces() -> Dict[str, Any]:
    """List all Kubernetes namespaces."""
    try:
        v1 = _core_api()
        namespaces = [ns.metadata.name for ns in v1.list_namespace().items]
        return {"namespaces": namespaces}
    except Exception as error:
        return {"error": _safe_message(error)}


@mcp.tool()
def list_pods(namespace: str = "default") -> Dict[str, Any]:
    """List pods in a namespace with their phase and node."""
    try:
        v1 = _core_api()
        pods = v1.list_namespaced_pod(namespace=namespace).items
        return {
            "namespace": namespace,
            "pods": [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "node": pod.spec.node_name,
                    "restarts": sum((s.restart_count or 0) for s in (pod.status.container_statuses or [])),
                }
                for pod in pods
            ],
        }
    except Exception as error:
        return {"error": _safe_message(error), "namespace": namespace}


@mcp.tool()
def pod_events(namespace: str, pod_name: str, limit: int = 20) -> Dict[str, Any]:
    """Get recent pod events sorted by last timestamp."""
    try:
        v1 = _core_api()
        field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
        event_items = v1.list_namespaced_event(namespace=namespace, field_selector=field_selector).items
        sorted_events = sorted(
            event_items,
            key=lambda e: e.last_timestamp or e.event_time or e.first_timestamp,
            reverse=True,
        )
        return {
            "namespace": namespace,
            "pod_name": pod_name,
            "events": [
                {
                    "type": event.type,
                    "reason": event.reason,
                    "message": event.message,
                    "count": event.count,
                    "last_timestamp": str(event.last_timestamp or event.event_time or event.first_timestamp),
                }
                for event in sorted_events[: max(1, limit)]
            ],
        }
    except Exception as error:
        return {"error": _safe_message(error), "namespace": namespace, "pod_name": pod_name}


@mcp.tool()
def pod_logs(namespace: str, pod_name: str, container: Optional[str] = None, tail: int = 200, previous: bool = False) -> Dict[str, Any]:
    """Read logs from a pod container."""
    try:
        v1 = _core_api()
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=max(1, tail),
            previous=previous,
        )
        return {
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
            "previous": previous,
            "logs": logs,
        }
    except Exception as error:
        return {"error": _safe_message(error), "namespace": namespace, "pod_name": pod_name}


@mcp.tool()
def node_conditions() -> Dict[str, Any]:
    """List nodes and unhealthy conditions."""
    try:
        v1 = _core_api()
        nodes = v1.list_node().items
        data = []
        for node in nodes:
            bad_conditions = []
            for cond in node.status.conditions or []:
                if cond.type in {"MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"} and cond.status == "True":
                    bad_conditions.append(
                        {"type": cond.type, "status": cond.status, "reason": cond.reason, "message": cond.message}
                    )
                if cond.type == "Ready" and cond.status != "True":
                    bad_conditions.append(
                        {"type": cond.type, "status": cond.status, "reason": cond.reason, "message": cond.message}
                    )
            data.append(
                {
                    "name": node.metadata.name,
                    "conditions": bad_conditions,
                }
            )
        return {"nodes": data}
    except Exception as error:
        return {"error": _safe_message(error)}


@mcp.tool()
def pod_diagnose(namespace: str, pod_name: str) -> Dict[str, Any]:
    """Diagnose common reasons why a pod is unhealthy."""
    try:
        v1 = _core_api()
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)

        field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
        event_items = v1.list_namespaced_event(namespace=namespace, field_selector=field_selector).items
        findings = _collect_pod_findings(pod, event_items)

        return {
            "namespace": namespace,
            "pod_name": pod_name,
            "phase": pod.status.phase,
            "pod_ip": pod.status.pod_ip,
            "node": pod.spec.node_name,
            "findings": findings,
            "suggested_next_steps": [
                "check warning events first",
                "inspect previous container logs for crash loops",
                "verify resource requests/limits and scheduling constraints",
            ],
        }
    except Exception as error:
        return {"error": _safe_message(error), "namespace": namespace, "pod_name": pod_name}


if __name__ == "__main__":
    mcp.run()

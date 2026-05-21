"""Scheduler dependency health checks."""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen


TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    kind: str
    target: Optional[str]
    required: bool = False


def _env_bool(
    name: str,
    default: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> bool:
    source = os.environ if env is None else env
    value = source.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _dependency_checks(
    env: Optional[Dict[str, str]] = None,
) -> List[DependencyCheck]:
    source = os.environ if env is None else env
    return [
        DependencyCheck(
            name="queue",
            kind="tcp" if source.get("SCHEDULER_QUEUE_TCP") else "http",
            target=source.get("SCHEDULER_QUEUE_TCP")
            or source.get("SCHEDULER_QUEUE_HEALTH_URL"),
            required=_env_bool("SCHEDULER_REQUIRE_QUEUE", env=source),
        ),
        DependencyCheck(
            name="storage",
            kind="tcp" if source.get("SCHEDULER_STORAGE_TCP") else "http",
            target=source.get("SCHEDULER_STORAGE_TCP")
            or source.get("SCHEDULER_STORAGE_HEALTH_URL"),
            required=_env_bool("SCHEDULER_REQUIRE_STORAGE", env=source),
        ),
    ]


def _split_host_port(target: str) -> Tuple[str, int]:
    host, separator, port = target.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {target!r}")
    return host, int(port)


def _check_tcp(target: str, timeout: float) -> Tuple[bool, str]:
    try:
        address = _split_host_port(target)
        with socket.create_connection(address, timeout=timeout):
            return True, "reachable"
    except OSError as exc:
        return False, str(exc)
    except ValueError as exc:
        return False, str(exc)


def _check_http(target: str, timeout: float) -> Tuple[bool, str]:
    request = Request(
        target,
        headers={"User-Agent": "agent-orchestrator-scheduler-health"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
    except URLError as exc:
        return False, str(exc.reason)
    except OSError as exc:
        return False, str(exc)

    if 200 <= status < 400:
        return True, f"status {status}"
    return False, f"status {status}"


def check_scheduler_dependencies(
    env: Optional[Dict[str, str]] = None,
    checks: Optional[Iterable[DependencyCheck]] = None,
) -> Tuple[bool, Dict[str, object]]:
    source = os.environ if env is None else env
    timeout = float(source.get("SCHEDULER_HEALTH_TIMEOUT", "2"))
    results = []
    healthy = True

    for check in checks or _dependency_checks(source):
        if not check.target:
            ok = not check.required
            detail = (
                "not configured"
                if ok
                else "required dependency is not configured"
            )
        elif check.kind == "tcp":
            ok, detail = _check_tcp(check.target, timeout)
        else:
            ok, detail = _check_http(check.target, timeout)

        healthy = healthy and ok
        results.append(
            {
                "name": check.name,
                "kind": check.kind,
                "target": check.target,
                "ok": ok,
                "detail": detail,
            }
        )

    return healthy, {
        "status": "healthy" if healthy else "unhealthy",
        "dependencies": results,
    }


def main() -> int:
    healthy, payload = check_scheduler_dependencies()
    print(json.dumps(payload, sort_keys=True))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())

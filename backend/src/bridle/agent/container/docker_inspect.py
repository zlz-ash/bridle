"""Rebuild container identity from Docker inspect payloads."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from bridle.agent.container.runner import ContainerMount, ContainerRequest

logger = logging.getLogger("bridle")


def memory_bytes(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    raw = str(value).strip().lower()
    if not raw:
        return 0
    if raw[-1] in {"b", "k", "m", "g"}:
        unit = raw[-1]
        amount = float(raw[:-1])
        scale = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]
        return int(amount * scale)
    return int(float(raw))


def format_memory_bytes(value: int) -> str:
    if value <= 0:
        return "0"
    if value % (1024**3) == 0:
        return f"{value // (1024**3)}g"
    if value % (1024**2) == 0:
        return f"{value // (1024**2)}m"
    if value % 1024 == 0:
        return f"{value // 1024}k"
    return str(value)


def cpus_from_nano(nano: int | None) -> str:
    if not nano:
        return "0"
    return f"{nano / 1_000_000_000:.1f}"


def cpus_to_nano(cpus: str) -> int:
    return int(float(cpus) * 1_000_000_000)


def normalize_cap_drop(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(sorted(str(item).upper() for item in values))


def normalize_security_opt(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not values:
        return ()
    normalized: list[str] = []
    for item in values:
        token = str(item).lower().split(":", 1)[0]
        normalized.append(token)
    return tuple(sorted(normalized))


def request_from_inspect_data(data: dict[str, Any]) -> ContainerRequest | None:
    if not data:
        return None
    config = data.get("Config") or {}
    host_config = data.get("HostConfig") or {}
    labels = dict(config.get("Labels") or {})
    mounts: list[ContainerMount] = []
    for mount in data.get("Mounts") or []:
        source = mount.get("Source")
        target = mount.get("Destination")
        if not source or not target:
            continue
        readonly = not bool(mount.get("RW", True))
        mounts.append(ContainerMount(source=Path(source), target=str(target), readonly=readonly))
    network_mode = host_config.get("NetworkMode") or "default"
    if network_mode == "default":
        network_mode = "bridge"
    name = str(data.get("Name") or config.get("Hostname") or "").lstrip("/")
    command = config.get("Cmd") or []
    if isinstance(command, str):
        command = [command]
    keep_alive = "keep-alive" in " ".join(str(x) for x in command)
    image_id = str(data.get("Image") or "")
    if image_id and not image_id.startswith("sha256:"):
        image_id = f"sha256:{image_id}"
    run_user = str(config.get("User") or "")
    memory_raw = host_config.get("Memory")
    nano_cpus = host_config.get("NanoCpus")
    pids_limit = host_config.get("PidsLimit")
    return ContainerRequest(
        name=name,
        image=str(config.get("Image") or ""),
        image_id=image_id,
        run_user=run_user,
        network_mode="none" if network_mode == "none" else "bridge",
        mounts=mounts,
        labels=labels,
        command=[str(x) for x in command],
        module_id=str(labels.get("bridle.module") or ""),
        boundary_fingerprint=str(labels.get("bridle.boundary_fp") or ""),
        image_version=str(labels.get("bridle.image_version") or "local"),
        keep_alive=keep_alive,
        read_only_root=bool(host_config.get("ReadonlyRootfs")),
        privileged=bool(host_config.get("Privileged")),
        cap_drop=normalize_cap_drop(host_config.get("CapDrop") or []),
        security_opt=normalize_security_opt(host_config.get("SecurityOpt") or []),
        pids_limit=int(pids_limit) if pids_limit is not None else 0,
        memory=format_memory_bytes(int(memory_raw)) if memory_raw else "0",
        cpus=cpus_from_nano(int(nano_cpus)) if nano_cpus else "0",
    )


def _parse_docker_inspect_json(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sanitized = re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r"\\\\", raw)
        try:
            payload = json.loads(sanitized)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload


def inspect_container_request(
    *,
    executable: str,
    container_id: str,
    timeout: int = 30,
) -> ContainerRequest | None:
    try:
        result = subprocess.run(
            [executable, "inspect", "--format", "{{json .}}", container_id],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info(
            "docker_inspect_failed",
            extra={
                "action": "docker_inspect_failed",
                "status": "failed",
                "detail": {"container_id": container_id, "error": str(exc)},
            },
        )
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    payload = _parse_docker_inspect_json(result.stdout)
    if payload is None:
        return None
    return request_from_inspect_data(payload)

#!/usr/bin/env python3
"""Per-run isolated Docker daemon lifecycle for candidate workers."""
from __future__ import annotations

import logging
import subprocess
import time
import uuid

LOGGER = logging.getLogger("bridle.isolated_docker")


class IsolatedDockerError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


def _run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def start_isolated_daemon(*, run_id: str | None = None) -> tuple[str, str, str]:
    owner = run_id or uuid.uuid4().hex[:12]
    network = f"bridle-net-{owner}"
    dind_name = f"bridle-dind-{owner}"
    create_net = _run(["docker", "network", "create", network])
    if create_net.returncode != 0:
        raise IsolatedDockerError("isolated_docker_network_create_failed", detail=create_net.stderr.strip())
    create_dind = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            dind_name,
            "--network",
            network,
            "--privileged",
            "-e",
            "DOCKER_TLS_CERTDIR=",
            "docker:24-dind",
            "dockerd",
            "--host=unix:///var/run/docker.sock",
            "--host=tcp://0.0.0.0:2375",
        ],
        timeout=120,
    )
    if create_dind.returncode != 0:
        _run(["docker", "network", "rm", network])
        raise IsolatedDockerError("isolated_docker_dind_start_failed", detail=create_dind.stderr.strip())
    ready = False
    for _ in range(60):
        probe = _run(["docker", "exec", dind_name, "docker", "info"], timeout=15)
        if probe.returncode == 0:
            ready = True
            break
        time.sleep(1.0)
    if not ready:
        stop_isolated_daemon(network=network, dind_name=dind_name)
        raise IsolatedDockerError("isolated_docker_dind_not_ready", detail=dind_name)
    docker_host = f"tcp://{dind_name}:2375"
    LOGGER.info("isolated_docker_started network=%s dind=%s host=%s", network, dind_name, docker_host)
    return docker_host, network, dind_name


def stop_isolated_daemon(*, network: str, dind_name: str) -> None:
    stop = _run(["docker", "stop", "-t", "5", dind_name], timeout=30)
    if stop.returncode != 0:
        LOGGER.warning("isolated_docker_stop_failed name=%s detail=%s", dind_name, stop.stderr.strip())
    rm = _run(["docker", "rm", dind_name], timeout=30)
    if rm.returncode != 0 and "No such container" not in (rm.stderr or ""):
        LOGGER.warning("isolated_docker_remove_failed name=%s detail=%s", dind_name, rm.stderr.strip())
    net_rm = _run(["docker", "network", "rm", network], timeout=30)
    if net_rm.returncode != 0 and "No such network" not in (net_rm.stderr or ""):
        LOGGER.warning("isolated_docker_network_remove_failed name=%s detail=%s", network, net_rm.stderr.strip())


def _normalize_image_id(raw: str) -> str:
    text = raw.strip()
    if text.startswith("sha256:"):
        return text
    if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text):
        return f"sha256:{text}"
    return text


def _run_bytes(args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False)


def import_host_image_to_dind(
    *,
    dind_name: str,
    image_ref: str,
    expected_digest: str | None = None,
) -> str:
    save = _run_bytes(["docker", "save", image_ref], timeout=600)
    if save.returncode != 0:
        detail = save.stderr.decode("utf-8", errors="replace").strip()
        raise IsolatedDockerError("isolated_docker_image_save_failed", detail=detail)
    load = subprocess.run(
        ["docker", "exec", "-i", dind_name, "docker", "load"],
        input=save.stdout,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if load.returncode != 0:
        detail = (load.stderr or load.stdout or b"").decode("utf-8", errors="replace").strip()
        raise IsolatedDockerError("isolated_docker_image_load_failed", detail=detail)
    inspect = _run(
        ["docker", "exec", dind_name, "docker", "image", "inspect", "-f", "{{.Id}}", image_ref],
        timeout=60,
    )
    if inspect.returncode != 0:
        raise IsolatedDockerError(
            "isolated_docker_image_inspect_failed",
            detail=(inspect.stderr or inspect.stdout).strip(),
        )
    loaded_digest = _normalize_image_id((inspect.stdout or "").strip())
    if expected_digest and _normalize_image_id(expected_digest) != loaded_digest:
        raise IsolatedDockerError(
            "isolated_docker_image_digest_mismatch",
            detail=f"expected={expected_digest} loaded={loaded_digest}",
        )
    LOGGER.info(
        "isolated_docker_image_imported dind=%s image=%s digest=%s",
        dind_name,
        image_ref,
        loaded_digest,
    )
    return loaded_digest

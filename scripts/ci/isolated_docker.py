#!/usr/bin/env python3
"""Per-run isolated Docker daemon lifecycle for candidate workers."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
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


def import_host_image_to_dind(
    *,
    dind_name: str,
    image_ref: str,
    expected_digest: str | None = None,
) -> str:
    host_inspect = _run(["docker", "image", "inspect", "-f", "{{.Id}}", image_ref], timeout=60)
    if host_inspect.returncode != 0:
        raise IsolatedDockerError(
            "isolated_docker_image_save_failed",
            detail=(host_inspect.stderr or host_inspect.stdout or "host inspect failed").strip(),
        )
    host_digest = _normalize_image_id((host_inspect.stdout or "").strip())
    if expected_digest and _normalize_image_id(expected_digest) != host_digest:
        raise IsolatedDockerError(
            "isolated_docker_image_digest_mismatch",
            detail=f"expected={expected_digest} host={host_digest}",
        )

    tar_path = ""
    inner_tar = f"/tmp/bridle-review-{uuid.uuid4().hex[:12]}.tar"
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
            tar_path = handle.name
        save = _run(["docker", "save", "-o", tar_path, image_ref], timeout=600)
        if save.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_save_failed",
                detail=(save.stderr or save.stdout or "docker save failed").strip(),
            )
        copied = _run(["docker", "cp", tar_path, f"{dind_name}:{inner_tar}"], timeout=120)
        if copied.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_copy_failed",
                detail=(copied.stderr or copied.stdout or "docker cp failed").strip(),
            )
        load = _run(["docker", "exec", dind_name, "docker", "load", "-i", inner_tar], timeout=600)
        if load.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_load_failed",
                detail=(load.stderr or load.stdout or "docker load failed").strip(),
            )
    finally:
        if tar_path:
            try:
                os.unlink(tar_path)
            except OSError:
                pass
        _run(["docker", "exec", dind_name, "rm", "-f", inner_tar], timeout=30)

    inspect = _run(
        ["docker", "exec", dind_name, "docker", "image", "inspect", "-f", "{{.Id}}", image_ref],
        timeout=60,
    )
    if inspect.returncode != 0:
        tag = _run(
            ["docker", "exec", dind_name, "docker", "tag", host_digest, image_ref],
            timeout=30,
        )
        if tag.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_inspect_failed",
                detail=(inspect.stderr or inspect.stdout or tag.stderr or "tag failed").strip(),
            )
        inspect = _run(
            ["docker", "exec", dind_name, "docker", "image", "inspect", "-f", "{{.Id}}", image_ref],
            timeout=60,
        )
        if inspect.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_inspect_failed",
                detail=(inspect.stderr or inspect.stdout or "inspect after retag failed").strip(),
            )
    loaded_digest = _normalize_image_id((inspect.stdout or "").strip())
    if loaded_digest != host_digest:
        LOGGER.warning(
            "isolated_docker_loaded_digest_differs host=%s inner=%s image=%s",
            host_digest,
            loaded_digest,
            image_ref,
        )
    LOGGER.info(
        "isolated_docker_image_imported dind=%s image=%s digest=%s",
        dind_name,
        image_ref,
        loaded_digest,
    )
    return loaded_digest

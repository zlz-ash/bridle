#!/usr/bin/env python3
"""Per-run isolated Docker daemon lifecycle for candidate workers."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

LOGGER = logging.getLogger("bridle.isolated_docker")
INNER_CANDIDATE_ROOT = "/bridle-candidate"


class IsolatedDockerError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


def _run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _candidate_bind_mounts(host_candidate: str) -> list[str]:
    """Expose the host checkout inside DinD at a fixed inner root (nested bind-safe)."""
    return [
        "--mount",
        f"type=bind,source={host_candidate},target={INNER_CANDIDATE_ROOT},bind-propagation=rshared",
    ]


def _ensure_candidate_mount_propagation(*, dind_name: str) -> None:
    for flag in ("--make-rshared", "--make-shared"):
        result = _run(
            ["docker", "exec", dind_name, "mount", flag, INNER_CANDIDATE_ROOT],
            timeout=30,
        )
        if result.returncode == 0:
            LOGGER.info("isolated_docker_candidate_mount_propagation flag=%s", flag)
            return
    LOGGER.warning(
        "isolated_docker_candidate_mount_propagation_failed detail=%s",
        (result.stderr or result.stdout or "mount propagation failed").strip(),
    )


def _host_prepare_candidate_mount(candidate_host_root: Path) -> None:
    if not hasattr(os, "getuid"):
        return
    candidate = candidate_host_root.resolve()
    for path in (candidate, candidate.parent):
        share = _run(["mount", "--make-shared", str(path)], timeout=30)
        if share.returncode == 0:
            LOGGER.info("isolated_docker_host_shared path=%s", path)
            return
    LOGGER.warning(
        "isolated_docker_host_shared_failed path=%s detail=%s",
        candidate,
        (share.stderr or share.stdout or "mount --make-shared failed").strip(),
    )


def _write_setup_transcript(payload: dict[str, str]) -> None:
    raw = os.environ.get("BRIDLE_DOCKER_EVIDENCE_DIR", "").strip()
    if not raw:
        return
    path = Path(raw) / "isolated-setup-transcript.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _staging_tar_path(suffix: str) -> Path:
    for key in ("BRIDLE_STAGING_ROOT", "BRIDLE_RUNNER_TEMP", "RUNNER_TEMP", "TMPDIR"):
        root = os.environ.get(key, "").strip()
        if root:
            staging = Path(root)
            staging.mkdir(parents=True, exist_ok=True)
            return staging / f"bridle-review-{uuid.uuid4().hex[:12]}{suffix}"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    return Path(handle.name)


def start_isolated_daemon(
    *,
    run_id: str | None = None,
    candidate_host_root: Path | None = None,
) -> tuple[str, str, str]:
    owner = run_id or uuid.uuid4().hex[:12]
    network = f"bridle-net-{owner}"
    dind_name = f"bridle-dind-{owner}"
    create_net = _run(["docker", "network", "create", network])
    if create_net.returncode != 0:
        raise IsolatedDockerError("isolated_docker_network_create_failed", detail=create_net.stderr.strip())
    volume_args: list[str] = []
    if candidate_host_root is not None:
        _host_prepare_candidate_mount(candidate_host_root)
        host_candidate = str(candidate_host_root.resolve())
        volume_args.extend(_candidate_bind_mounts(host_candidate))
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
            *volume_args,
            "-e",
            "DOCKER_TLS_CERTDIR=",
            "docker:24-dind",
            "dockerd",
            "--storage-driver=vfs",
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
    if candidate_host_root is not None:
        _ensure_candidate_mount_propagation(dind_name=dind_name)
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


def _parse_docker_load_id(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Loaded image ID:"):
            return _normalize_image_id(line.removeprefix("Loaded image ID:").strip())
    return None


def _is_image_digest(value: str) -> bool:
    normalized = _normalize_image_id(value)
    return normalized.startswith("sha256:") and len(normalized) == 71


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

    tar_path = _staging_tar_path(".tar")
    loaded_from_output: str | None = None
    try:
        save = _run(["docker", "save", "-o", str(tar_path), image_ref], timeout=600)
        if save.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_save_failed",
                detail=(save.stderr or save.stdout or "docker save failed").strip(),
            )
        if not tar_path.is_file() or tar_path.stat().st_size == 0:
            raise IsolatedDockerError(
                "isolated_docker_image_save_failed",
                detail=f"saved tar missing or empty path={tar_path}",
            )
        with tar_path.open("rb") as tar_handle:
            load = subprocess.run(
                ["docker", "exec", "-i", dind_name, "docker", "load"],
                stdin=tar_handle,
                capture_output=True,
                timeout=600,
                check=False,
            )
        if load.returncode != 0:
            raise IsolatedDockerError(
                "isolated_docker_image_load_failed",
                detail=(load.stderr or load.stdout or b"docker load failed")
                .decode("utf-8", errors="replace")
                .strip(),
            )
        loaded_from_output = _parse_docker_load_id(
            f"{(load.stdout or b'').decode('utf-8', errors='replace')}\n"
            f"{(load.stderr or b'').decode('utf-8', errors='replace')}"
        )
        if (
            loaded_from_output
            and _is_image_digest(loaded_from_output)
            and loaded_from_output != host_digest
        ):
            raise IsolatedDockerError(
                "isolated_docker_loaded_digest_mismatch",
                detail=f"host={host_digest} loaded={loaded_from_output} image={image_ref}",
            )
    finally:
        try:
            tar_path.unlink(missing_ok=True)
        except OSError:
            pass

    inspect = _run(
        ["docker", "exec", dind_name, "docker", "image", "inspect", "-f", "{{.Id}}", image_ref],
        timeout=60,
    )
    if inspect.returncode != 0:
        tag_source = loaded_from_output or host_digest
        tag = _run(
            ["docker", "exec", dind_name, "docker", "tag", tag_source, image_ref],
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
        raise IsolatedDockerError(
            "isolated_docker_loaded_digest_mismatch",
            detail=f"host={host_digest} inner={loaded_digest} image={image_ref}",
        )
    LOGGER.info(
        "isolated_docker_image_imported dind=%s image=%s digest=%s",
        dind_name,
        image_ref,
        loaded_digest,
    )
    return loaded_digest


def verify_worker_docker_access(
    *,
    dind_name: str,
    network: str,
    image_ref: str,
    worker_image: str,
    candidate_host_root: Path | None = None,
) -> None:
    docker_host = f"tcp://{dind_name}:2375"
    inner_info = _run(["docker", "exec", dind_name, "docker", "info"], timeout=120)
    if inner_info.returncode != 0:
        raise IsolatedDockerError(
            "isolated_docker_inner_daemon_unreachable",
            detail=(inner_info.stderr or inner_info.stdout or "inner docker info failed").strip(),
        )
    inspect = _run(
        ["docker", "exec", dind_name, "docker", "image", "inspect", "-f", "{{.Id}}", image_ref],
        timeout=60,
    )
    if inspect.returncode != 0:
        raise IsolatedDockerError(
            "isolated_docker_review_image_missing",
            detail=(inspect.stderr or inspect.stdout or f"missing image {image_ref}").strip(),
        )
    if candidate_host_root is not None:
        probe_root = candidate_host_root / ".bridle-dind-bind-probe"
        for subdir in ("project", "baseline", "mocks", "output", "diagnostics"):
            (probe_root / subdir).mkdir(parents=True, exist_ok=True)
        (probe_root / "project" / "marker.txt").write_text("ok\n", encoding="utf-8")
        probe_base = f"{INNER_CANDIDATE_ROOT}/.bridle-dind-bind-probe"
        deep_probe = f"{INNER_CANDIDATE_ROOT}/backend/.bridle-dind-bind-probe-deep"
        deep_host = candidate_host_root / "backend" / ".bridle-dind-bind-probe-deep" / "project"
        deep_host.mkdir(parents=True, exist_ok=True)
        (deep_host / "marker.txt").write_text("deep\n", encoding="utf-8")
        try:
            bind_probe = _run(
                [
                    "docker",
                    "exec",
                    dind_name,
                    "docker",
                    "create",
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m",
                    "--user",
                    "1000",
                    "--mount",
                    f"type=bind,src={probe_base}/project,dst=/workspace/project",
                    "--mount",
                    f"type=bind,src={probe_base}/baseline,dst=/workspace/baseline,readonly",
                    "--mount",
                    f"type=bind,src={probe_base}/mocks,dst=/workspace/mocks,readonly",
                    "--mount",
                    f"type=bind,src={probe_base}/output,dst=/workspace/output",
                    "--mount",
                    f"type=bind,src={probe_base}/diagnostics,dst=/workspace/diagnostics",
                    image_ref,
                    "python",
                    "-m",
                    "bridle.agent.container.entrypoint",
                    "--keep-alive",
                ],
                timeout=120,
            )
            if bind_probe.returncode != 0:
                raise IsolatedDockerError(
                    "isolated_docker_bind_mount_probe_failed",
                    detail=(bind_probe.stderr or bind_probe.stdout or "bind create failed").strip(),
                )
            container_id = (bind_probe.stdout or "").strip()
            if container_id:
                _run(["docker", "exec", dind_name, "docker", "rm", container_id], timeout=60)
            deep_probe_run = _run(
                [
                    "docker",
                    "exec",
                    dind_name,
                    "docker",
                    "create",
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,src={deep_probe}/project,dst=/workspace/project",
                    image_ref,
                    "true",
                ],
                timeout=120,
            )
            if deep_probe_run.returncode != 0:
                raise IsolatedDockerError(
                    "isolated_docker_deep_bind_probe_failed",
                    detail=(deep_probe_run.stderr or deep_probe_run.stdout or "deep bind create failed").strip(),
                )
            deep_container_id = (deep_probe_run.stdout or "").strip()
            if deep_container_id:
                _run(["docker", "exec", dind_name, "docker", "rm", deep_container_id], timeout=60)
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)
            shutil.rmtree(deep_host.parent, ignore_errors=True)
    worker_probe_name = f"bridle-dind-worker-probe-{uuid.uuid4().hex[:12]}"
    worker_probe_args = [
        "docker",
        "run",
        "--rm",
        "--name",
        worker_probe_name,
        "--network",
        network,
    ]
    if candidate_host_root is not None:
        host_candidate = str(candidate_host_root.resolve())
        worker_probe_args.extend(
            [
                "--mount",
                f"type=bind,source={host_candidate},target={INNER_CANDIDATE_ROOT},bind-propagation=rshared",
            ]
        )
    else:
        worker_probe_args.extend(["--volumes-from", f"{dind_name}:rw"])
    run_uid = os.getuid() if hasattr(os, "getuid") else 1000
    run_gid = os.getgid() if hasattr(os, "getgid") else 1000
    worker_probe = _run(
        [
            *worker_probe_args,
            "-u",
            f"{run_uid}:{run_gid}",
            "-e",
            f"DOCKER_HOST={docker_host}",
            worker_image,
            "docker",
            "info",
        ],
        timeout=120,
    )
    if worker_probe.returncode != 0:
        raise IsolatedDockerError(
            "isolated_docker_worker_network_unreachable",
            detail=(worker_probe.stderr or worker_probe.stdout or "worker docker info failed").strip(),
        )
    LOGGER.info(
        "isolated_docker_worker_access_verified network=%s dind=%s image=%s digest=%s",
        network,
        dind_name,
        image_ref,
        (inspect.stdout or "").strip(),
    )

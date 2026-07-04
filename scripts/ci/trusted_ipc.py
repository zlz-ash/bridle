#!/usr/bin/env python3
"""Typed IPC envelopes between trusted controller and candidate worker (v2)."""
from __future__ import annotations

import json
from dataclasses import dataclass

IPC_SCHEMA = "bridle.trusted_ipc/v2"
WORKER_STATE_EXITED = "exited"
WORKER_STATE_TIMED_OUT = "timed_out"
WORKER_STATE_FAILED_BEFORE_EXEC = "failed_before_exec"

PUBLIC_ENV_ALLOWLIST = frozenset(
    {
        "BRIDLE_TRUSTED_CHECKOUT_ROOT",
        "BRIDLE_RUN_DOCKER_TESTS",
        "BRIDLE_AGENT_IMAGE",
        "BRIDLE_REVIEW_SOURCE_DIGEST",
        "BRIDLE_REVIEW_IMAGE_DIGEST",
        "GITHUB_SHA",
        "BRIDLE_CANDIDATE_WORKER",
        "BRIDLE_ISOLATION_PROBE",
        "BRIDLE_RUN_LEASE_ID",
        "BRIDLE_IT_RUN_ID",
        "DOCKER_HOST",
    }
)
MAX_STREAM_BYTES = 1_048_576


class TrustedIpcError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


@dataclass(frozen=True)
class WorkerRequest:
    candidate_root: str
    trusted_config: str
    pytest_args: tuple[str, ...]
    public_env: dict[str, str]


@dataclass(frozen=True)
class WorkerObservation:
    worker_state: str
    exit_code: int | None
    stdout: str
    stderr: str
    truncated_stdout: bool
    truncated_stderr: bool
    worker_pid: int | None
    worker_uid: int | None
    controller_pid: int
    controller_uid: int | None


def _sanitize_public_env(raw: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in raw.items() if key in PUBLIC_ENV_ALLOWLIST}


def encode_request(request: WorkerRequest) -> str:
    payload = {
        "schema": IPC_SCHEMA,
        "candidate_root": request.candidate_root,
        "trusted_config": request.trusted_config,
        "pytest_args": list(request.pytest_args),
        "public_env": _sanitize_public_env(request.public_env),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def decode_request(raw: str) -> WorkerRequest:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrustedIpcError("trusted_ipc_request_invalid", detail=str(exc)) from exc
    if not isinstance(payload, dict) or payload.get("schema") != IPC_SCHEMA:
        raise TrustedIpcError("trusted_ipc_request_invalid")
    for field in ("candidate_root", "trusted_config"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise TrustedIpcError("trusted_ipc_request_invalid", detail=field)
    pytest_args = payload.get("pytest_args")
    if not isinstance(pytest_args, list) or not all(isinstance(item, str) for item in pytest_args):
        raise TrustedIpcError("trusted_ipc_request_invalid", detail="pytest_args")
    public_env = payload.get("public_env")
    if not isinstance(public_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in public_env.items()
    ):
        raise TrustedIpcError("trusted_ipc_request_invalid", detail="public_env")
    return WorkerRequest(
        candidate_root=str(payload["candidate_root"]).strip(),
        trusted_config=str(payload["trusted_config"]).strip(),
        pytest_args=tuple(pytest_args),
        public_env=_sanitize_public_env(public_env),
    )


def encode_observation(observation: WorkerObservation) -> str:
    payload = {
        "schema": IPC_SCHEMA,
        "worker_state": observation.worker_state,
        "exit_code": observation.exit_code,
        "stdout": observation.stdout,
        "stderr": observation.stderr,
        "truncated_stdout": observation.truncated_stdout,
        "truncated_stderr": observation.truncated_stderr,
        "worker_pid": observation.worker_pid,
        "worker_uid": observation.worker_uid,
        "controller_pid": observation.controller_pid,
        "controller_uid": observation.controller_uid,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def decode_observation(raw: str) -> WorkerObservation:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail=str(exc)) from exc
    if not isinstance(payload, dict) or payload.get("schema") != IPC_SCHEMA:
        raise TrustedIpcError("trusted_ipc_observation_invalid")
    worker_state = payload.get("worker_state")
    if worker_state not in {
        WORKER_STATE_EXITED,
        WORKER_STATE_TIMED_OUT,
        WORKER_STATE_FAILED_BEFORE_EXEC,
    }:
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="worker_state")
    exit_code = payload.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="exit_code")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="streams")
    for field in ("truncated_stdout", "truncated_stderr"):
        if not isinstance(payload.get(field), bool):
            raise TrustedIpcError("trusted_ipc_observation_invalid", detail=field)
    worker_pid = payload.get("worker_pid")
    if worker_pid is not None and (not isinstance(worker_pid, int) or isinstance(worker_pid, bool)):
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="worker_pid")
    worker_uid = payload.get("worker_uid")
    if worker_uid is not None and (not isinstance(worker_uid, int) or isinstance(worker_uid, bool)):
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="worker_uid")
    controller_pid = payload.get("controller_pid")
    if not isinstance(controller_pid, int) or isinstance(controller_pid, bool):
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="controller_pid")
    controller_uid = payload.get("controller_uid")
    if controller_uid is not None and (
        not isinstance(controller_uid, int) or isinstance(controller_uid, bool)
    ):
        raise TrustedIpcError("trusted_ipc_observation_invalid", detail="controller_uid")
    return WorkerObservation(
        worker_state=str(worker_state),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated_stdout=bool(payload["truncated_stdout"]),
        truncated_stderr=bool(payload["truncated_stderr"]),
        worker_pid=worker_pid,
        worker_uid=worker_uid,
        controller_pid=controller_pid,
        controller_uid=controller_uid,
    )

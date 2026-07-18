"""Controlled in-container test executor for candidate workspaces."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from bridle.agent.container.active_slot import (
    ActiveSlotLayout,
    read_lease,
    slot_layout,
    verify_lease_token,
)
from bridle.agent.container.candidate_path_guard import CandidatePathError, resolve_candidate_rel
from bridle.agent.container.container_control import (
    RESULT_SCHEMA,
    build_control_envelope,
    format_control_envelope_line,
)

_MANIFEST_SCHEMA = "bridle.container_test_request/v1"
_OUTPUT_SCHEMA = RESULT_SCHEMA
_MAX_OUTPUT_CHARS = 8192


def _container_slot_layout() -> ActiveSlotLayout:
    return ActiveSlotLayout(
        slot_root=Path("/workspace"),
        project=Path("/workspace/project"),
        baseline=Path("/workspace/baseline"),
        mocks=Path("/workspace/mocks"),
        output=Path("/workspace/output"),
        diagnostics=Path("/workspace/diagnostics"),
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scan_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not root.is_dir():
        return hashes
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest = _file_hash(path)
            if digest is not None:
                hashes[rel] = digest
    return hashes


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]", True


def _load_request_manifest(diagnostics_dir: Path) -> dict[str, Any]:
    path = diagnostics_dir / "test-request.json"
    if not path.is_file():
        raise ValueError("missing_test_request_manifest")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != _MANIFEST_SCHEMA:
        raise ValueError("unknown_test_request_schema")
    return payload


def _run_argv(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    pytest_result_path: Path | None = None,
) -> dict[str, Any]:
    original_argv = [str(x) for x in argv]
    executed_argv = list(original_argv)
    fake_host_argv = (
        os.name == "nt"
        and os.environ.get("BRIDLE_FAKE_CONTAINER_RUNNER") == "1"
        and len(executed_argv) == 3
        and executed_argv[:2] == ["bash", "-lc"]
    )
    if fake_host_argv:
        executed_argv = shlex.split(executed_argv[2], posix=True)
    if executed_argv and executed_argv[0] == "python":
        executed_argv[0] = sys.executable
    is_pytest = _is_pytest_argv(executed_argv)
    env = None
    if is_pytest and pytest_result_path is not None:
        pytest_result_path.unlink(missing_ok=True)
        executed_argv.extend(["-p", "bridle.agent.container.pytest_result_plugin"])
        env = dict(os.environ)
        env["BRIDLE_PYTEST_RESULT_PATH"] = str(pytest_result_path)
        env["BRIDLE_PYTEST_PROJECT_ROOT"] = str(cwd)
    started = time.monotonic()
    started_at = time.time()
    try:
        result = subprocess.run(
            executed_argv,
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        stdout, stdout_trunc = _truncate(result.stdout or "")
        stderr, stderr_trunc = _truncate(result.stderr or "")
        payload = {
            "argv": original_argv,
            "execution_adapter": "windows_fake_host_argv" if fake_host_argv else None,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "started_at": started_at,
            "finished_at": time.time(),
            "timed_out": False,
        }
        if is_pytest:
            payload.update(_load_pytest_result(pytest_result_path))
        return payload
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_trunc = _truncate(_as_text(exc.stdout))
        stderr, stderr_trunc = _truncate(_as_text(exc.stderr))
        return {
            "argv": original_argv,
            "exit_code": -1,
            "stdout": stdout,
            "stderr": stderr or f"Command timed out after {timeout_seconds}s",
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "started_at": started_at,
            "finished_at": time.time(),
            "timed_out": True,
            **(_load_pytest_result(pytest_result_path) if is_pytest else {}),
        }


def _is_pytest_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    if executable in {"pytest", "pytest.exe"}:
        return True
    return (
        executable in {"python", "python.exe"}
        and len(argv) >= 3
        and argv[1] == "-m"
        and argv[2].lower() == "pytest"
    )


def _load_pytest_result(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {
            "case_results": [],
            "collection_errors": [
                {"node_id": "", "message": "pytest_observer_result_missing"}
            ],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "case_results": [],
            "collection_errors": [
                {"node_id": "", "message": "pytest_observer_result_invalid"}
            ],
        }
    if payload.get("schema") != "bridle.pytest_case_results/v1":
        return {
            "case_results": [],
            "collection_errors": [
                {"node_id": "", "message": "pytest_observer_schema_invalid"}
            ],
        }
    return {
        "case_results": [dict(item) for item in payload.get("case_results") or []],
        "collection_errors": [
            dict(item) for item in payload.get("collection_errors") or []
        ],
    }


def run_active_slot_task(
    slot_root: str | Path | None = None,
    *,
    timeout_seconds: int = 300,
    lease_token: str | None = None,
) -> int:
    """Execute approved test commands using split active slot mounts."""
    layout = slot_layout(Path(slot_root)) if slot_root is not None else _container_slot_layout()
    token = lease_token or os.environ.get("BRIDLE_LEASE_TOKEN", "").strip()
    if token:
        try:
            verify_lease_token(layout, token=token)
        except CandidatePathError:
            return _write_failure(layout, error_code="active_slot_lease_mismatch", exit_code=2)
    return _run_task_at_layout(layout, timeout_seconds=timeout_seconds)


def run_container_task(
    module_root: str | Path = "/container",
    *,
    candidate_rel: str,
    timeout_seconds: int = 300,
) -> int:
    """Execute approved test commands for one candidate under a module mount root."""
    mount_root = Path(module_root)
    try:
        candidate_root = resolve_candidate_rel(mount_root, candidate_rel)
    except CandidatePathError as exc:
        fallback = mount_root / "candidates" / "_invalid"
        output_dir = fallback / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": _OUTPUT_SCHEMA,
            "status": "failed",
            "error_code": exc.error_code,
            "exit_code": 2,
            "results": [],
        }
        return _emit_task_result(
            ActiveSlotLayout(
                slot_root=fallback,
                project=fallback / "project",
                baseline=fallback / "baseline",
                mocks=fallback / "mocks",
                output=output_dir,
                diagnostics=fallback / "diagnostics",
            ),
            manifest,
            2,
            candidate_rel=candidate_rel,
        )
    layout = ActiveSlotLayout(
        slot_root=candidate_root,
        project=candidate_root / "project",
        baseline=candidate_root / "baseline",
        mocks=candidate_root / "mocks",
        output=candidate_root / "output",
        diagnostics=candidate_root / "diagnostics",
    )
    return _run_task_at_layout(layout, timeout_seconds=timeout_seconds, candidate_rel=candidate_rel)


def _run_task_at_layout(
    layout: ActiveSlotLayout,
    *,
    timeout_seconds: int,
    candidate_rel: str | None = None,
) -> int:
    layout.output.mkdir(parents=True, exist_ok=True)
    project_dir = layout.project
    baseline_dir = layout.baseline
    mocks_dir = layout.mocks
    cwd = project_dir if project_dir.is_dir() else layout.slot_root
    cwd.mkdir(parents=True, exist_ok=True)

    cached_candidate_rel = candidate_rel
    if cached_candidate_rel is None:
        try:
            cached_candidate_rel = read_lease(layout).candidate_rel
        except (OSError, CandidatePathError):
            cached_candidate_rel = None

    baseline_before = _scan_tree(baseline_dir)
    mocks_before = _scan_tree(mocks_dir)
    project_before = _scan_tree(project_dir)

    try:
        request = _load_request_manifest(layout.diagnostics)
    except ValueError as exc:
        return _write_failure(layout, error_code=str(exc), exit_code=2)

    protected = request.get("protected_hashes") or {}
    expected_baseline = protected.get("baseline")
    expected_mocks = protected.get("mocks")
    if expected_baseline is not None and baseline_before != expected_baseline:
        return _write_failure(
            layout,
            error_code="baseline_or_mock_tampered",
            exit_code=5,
            extra={"phase": "pre_exec", "surface": "baseline"},
        )
    if expected_mocks is not None and mocks_before != expected_mocks:
        return _write_failure(
            layout,
            error_code="baseline_or_mock_tampered",
            exit_code=5,
            extra={"phase": "pre_exec", "surface": "mocks"},
        )

    results: list[dict[str, Any]] = []
    exit_code = 0
    red_verification = request.get("red_verification") is True
    allowed_ids = {cmd.get("command_id") for cmd in request.get("commands") or []}
    for cmd in request.get("commands") or []:
        command_id = cmd.get("command_id")
        argv = [str(x) for x in cmd.get("argv") or []]
        if not argv or command_id not in allowed_ids:
            return _write_failure(
                layout,
                error_code="test_target_not_allowed",
                exit_code=3,
                results=results,
            )
        result = _run_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            pytest_result_path=layout.output / f"pytest-{command_id}.json",
        )
        result["command_id"] = command_id
        result["raw_command"] = cmd.get("raw_command", " ".join(argv))
        results.append(result)
        if result["exit_code"] != 0 or result.get("timed_out"):
            exit_code = int(result["exit_code"]) if result["exit_code"] is not None else -1
            if result.get("timed_out") or not red_verification:
                break

    baseline_after = _scan_tree(baseline_dir)
    mocks_after = _scan_tree(mocks_dir)
    if baseline_before != baseline_after or mocks_before != mocks_after:
        return _write_failure(
            layout,
            error_code="baseline_or_mock_tampered",
            exit_code=5,
            results=results,
            extra={
                "baseline_before": baseline_before,
                "baseline_after": baseline_after,
                "mocks_before": mocks_before,
                "mocks_after": mocks_after,
            },
        )

    project_after = _scan_tree(project_dir)
    write_set = set(str(p) for p in request.get("write_set") or [])
    changed_paths = sorted(
        rel
        for rel in set(project_before) | set(project_after)
        if project_before.get(rel) != project_after.get(rel)
    )
    out_of_scope = [
        rel
        for rel in changed_paths
        if rel not in write_set and not _is_ephemeral_test_artifact(rel)
    ]

    if out_of_scope:
        status = "failed"
        error_code = "out_of_scope_change"
        exit_code = 4
    elif exit_code != 0:
        status = "failed"
        error_code = "test_failed"
    else:
        status = "completed"
        error_code = None

    manifest = {
        "schema": _OUTPUT_SCHEMA,
        "status": status,
        "error_code": error_code,
        "exit_code": exit_code,
        "results": results,
        "changed_paths": changed_paths,
        "baseline_hashes": baseline_before,
        "candidate_hashes": project_after,
        "out_of_scope_changes": out_of_scope,
        "candidate_rel": cached_candidate_rel,
    }
    return _emit_task_result(layout, manifest, int(exit_code), candidate_rel=cached_candidate_rel)


def _write_failure(
    layout: ActiveSlotLayout,
    *,
    error_code: str,
    exit_code: int,
    results: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    manifest: dict[str, Any] = {
        "schema": _OUTPUT_SCHEMA,
        "status": "failed",
        "error_code": error_code,
        "exit_code": exit_code,
        "results": results or [],
    }
    if extra:
        manifest.update(extra)
    return _emit_task_result(layout, manifest, exit_code)


def _is_ephemeral_test_artifact(rel: str) -> bool:
    normalized = rel.replace("\\", "/")
    if normalized == ".pytest_cache" or normalized.startswith(".pytest_cache/"):
        return True
    return "/__pycache__/" in f"/{normalized}/" or normalized.endswith(".pyc")


def _emit_task_result(
    layout: ActiveSlotLayout,
    manifest: dict[str, Any],
    exit_code: int,
    *,
    candidate_rel: str | None = None,
) -> int:
    run_id = os.environ.get("BRIDLE_RUN_ID", "").strip()
    envelope = build_control_envelope(
        manifest=manifest,
        run_id=run_id,
        candidate_rel=candidate_rel,
        exit_code=int(exit_code),
    )
    line = format_control_envelope_line(envelope)
    print(line, flush=True)
    with contextlib.suppress(OSError):
        _write_manifest(layout.output, manifest)
    return int(exit_code)


def _write_manifest(output_dir: Path, manifest: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp = output_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(output_dir / "manifest.json")


def main() -> None:
    if "--keep-alive" in sys.argv:
        while True:
            time.sleep(3600)
    if "--run-task" in sys.argv:
        if os.environ.get("BRIDLE_ACTIVE_SLOT") == "1":
            slot_root = os.environ.get("BRIDLE_SLOT_ROOT", "").strip() or None
            timeout_raw = os.environ.get("BRIDLE_TASK_TIMEOUT", "300").strip()
            try:
                timeout_seconds = int(timeout_raw)
            except ValueError:
                timeout_seconds = 300
            raise SystemExit(
                run_active_slot_task(
                    slot_root,
                    timeout_seconds=timeout_seconds,
                    lease_token=os.environ.get("BRIDLE_LEASE_TOKEN"),
                )
            )
        rel = os.environ.get("BRIDLE_CANDIDATE_REL", "").strip()
        if not rel:
            raise SystemExit(2)
        raise SystemExit(run_container_task("/container", candidate_rel=rel))
    raise SystemExit(2)


if __name__ == "__main__":
    main()

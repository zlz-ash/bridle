#!/usr/bin/env python3
"""Trusted controller for candidate worker execution and post-run validation."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("bridle.trusted_test_runner")
INJECTABLE_ENV = frozenset(
    {
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sanitized_environment(source: Mapping[str, str]) -> dict[str, str]:
    result = {key: value for key, value in source.items() if key not in INJECTABLE_ENV}
    result["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return result


def trusted_config_path(trusted_root: Path) -> Path:
    return trusted_root / "backend/pyproject.toml"


def trusted_scripts_path(trusted_root: Path) -> Path:
    return trusted_root / "scripts/ci"


def build_public_env(*, candidate_root: Path, probe: bool) -> dict[str, str]:
    env: dict[str, str] = {
        "BRIDLE_TRUSTED_CHECKOUT_ROOT": str(candidate_root.resolve()),
        "BRIDLE_CANDIDATE_WORKER": "1",
    }
    if probe:
        env["BRIDLE_ISOLATION_PROBE"] = "1"
    for key in (
        "BRIDLE_RUN_DOCKER_TESTS",
        "BRIDLE_AGENT_IMAGE",
        "BRIDLE_REVIEW_SOURCE_DIGEST",
        "BRIDLE_REVIEW_IMAGE_DIGEST",
        "BRIDLE_IT_RUN_ID",
        "GITHUB_SHA",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            env[key] = value
    return env


def verify_controller_state(
    *,
    before_pytest: Any,
    harness_before: str,
    harness_path: Path,
    evidence_dir: Path | None,
    probe_report: dict[str, Any] | None,
) -> None:
    if before_pytest is not None and sys.modules.get("pytest") is not before_pytest:
        raise RuntimeError("trusted_controller_pytest_tampered")
    if harness_path.exists() and harness_path.read_text(encoding="utf-8") != harness_before:
        raise RuntimeError("trusted_controller_harness_overridden")
    if evidence_dir is not None:
        forged = evidence_dir / "malicious-evidence.json"
        if forged.exists():
            raise RuntimeError(f"trusted_controller_evidence_forged path={forged}")
    if probe_report is None:
        return
    if probe_report.get("control_env_read", {}).get("succeeded"):
        raise RuntimeError("candidate_leaked_control_env")
    evidence_root = evidence_dir.resolve().as_posix() if evidence_dir is not None else ""
    for item in probe_report.get("evidence_write", {}).get("outcomes") or []:
        if not item.get("succeeded"):
            continue
        path = str(item.get("path") or "")
        if path.startswith("/trusted-config") or path.startswith("/trusted-scripts"):
            raise RuntimeError(f"candidate_wrote_blocked_path path={path}")
        if evidence_root and path.startswith(evidence_root):
            raise RuntimeError(f"candidate_wrote_blocked_path path={path}")
    for item in probe_report.get("harness_override", {}).get("outcomes") or []:
        if item.get("succeeded"):
            raise RuntimeError(f"candidate_overrode_harness path={item.get('path')}")


def emit_ci_phase(phase: str, *, detail: str = "") -> None:
    message = f"BRIDLE_CI_PHASE={phase}"
    if detail:
        message = f"{message} detail={detail}"
    print(message, file=sys.stderr)
    LOGGER.info(message)


def verify_worker_observation(observation: Any, *, probe: bool) -> None:
    if observation.worker_state not in {"exited", "timed_out", "failed_before_exec"}:
        raise RuntimeError(f"worker_state_invalid state={observation.worker_state}")
    if observation.truncated_stdout or observation.truncated_stderr:
        raise RuntimeError("worker_stream_truncated")
    if observation.worker_state == "exited":
        if observation.exit_code is None or not isinstance(observation.exit_code, int):
            raise RuntimeError("worker_exit_code_missing")
    elif observation.exit_code is not None:
        raise RuntimeError("worker_exit_code_for_non_exited_state")
    if probe:
        if observation.worker_state != "exited":
            raise RuntimeError(f"probe_worker_state_invalid state={observation.worker_state}")
        if observation.exit_code not in (0, 5):
            raise RuntimeError(f"worker_exit_nonzero code={observation.exit_code}")
        return
    if observation.worker_state != "exited":
        if observation.worker_state == "failed_before_exec":
            raise RuntimeError(
                f"worker_failed_before_exec exit_code={observation.exit_code} "
                f"stderr={observation.stderr[-4000:]}"
            )
        raise RuntimeError(f"worker_state_not_successful state={observation.worker_state}")


def emit_worker_streams(worker_stdout: str, worker_stderr: str, *, limit: int = 12000) -> None:
    if worker_stderr.strip():
        print("--- worker stderr ---", file=sys.stderr)
        print(worker_stderr[-limit:], file=sys.stderr)
    if worker_stdout.strip():
        print("--- worker stdout ---", file=sys.stderr)
        print(worker_stdout[-limit:], file=sys.stderr)


def write_controller_failure_transcript(
    *,
    error: str,
    worker_stdout: str,
    worker_stderr: str,
    observation: Any | None = None,
) -> None:
    evidence_dir = os.environ.get("BRIDLE_DOCKER_EVIDENCE_DIR", "").strip()
    if not evidence_dir:
        return
    payload = {
        "error": error,
        "worker_state": getattr(observation, "worker_state", None) if observation else None,
        "exit_code": getattr(observation, "exit_code", None) if observation else None,
        "worker_stdout_tail": worker_stdout[-8000:],
        "worker_stderr_tail": worker_stderr[-8000:],
    }
    path = Path(evidence_dir) / "controller-failure.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def controller_ipc_dir(source_env: Mapping[str, str]) -> Path | None:
    raw = source_env.get("BRIDLE_CONTROLLER_IPC_DIR", "").strip()
    return Path(raw) if raw else None


def make_stream_handler(*, ctx: Any, trusted_root: Path):
    script_dir = trusted_scripts_path(trusted_root)
    controller = _load_module("bridle_trusted_evidence_controller", script_dir / "trusted_evidence_controller.py")

    def handle_line(line: str) -> None:
        controller.handle_controller_line(
            line,
            ctx=ctx,
            trusted_scripts=script_dir,
        )

    return handle_line


def setup_probe_layout(candidate_root: Path, trusted_harness_root: Path, evidence_dir: Path | None) -> None:
    script_dir = trusted_scripts_path(trusted_harness_root)
    probe_module = _load_module("bridle_candidate_isolation_probe", script_dir / "candidate_isolation_probe.py")
    probe_module.write_probe_files(candidate_root)
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_link = candidate_root.parent / "evidence"
        if not evidence_link.exists():
            try:
                evidence_link.symlink_to(evidence_dir, target_is_directory=True)
            except OSError:
                pass
    harness_link = candidate_root.parent / "trusted-harness"
    if not harness_link.exists():
        try:
            harness_link.symlink_to(trusted_harness_root.resolve(), target_is_directory=True)
        except OSError:
            pass


def cleanup_probe_layout(candidate_root: Path, evidence_dir: Path | None) -> None:
    probe_root = candidate_root / ".bridle-isolation-probe"
    if probe_root.exists():
        shutil.rmtree(probe_root)
    evidence_link = candidate_root.parent / "evidence"
    if evidence_link.is_symlink():
        evidence_link.unlink()
    harness_link = candidate_root.parent / "trusted-harness"
    if harness_link.is_symlink():
        harness_link.unlink()
    if evidence_dir is not None:
        forged = evidence_dir / "malicious-evidence.json"
        if forged.is_file():
            forged.unlink()


def run_worker(
    *,
    candidate_root: Path,
    trusted_root: Path,
    pytest_args: Sequence[str],
    probe: bool,
    controller_ipc: Path | None = None,
    ctx: Any | None = None,
    isolated=None,
):
    script_dir = trusted_scripts_path(trusted_root)
    worker_sandbox = _load_module("bridle_worker_sandbox", script_dir / "worker_sandbox.py")
    config_path = trusted_config_path(trusted_root)
    if probe:
        probe_config = script_dir / "protected/pytest-probe.toml"
        if probe_config.is_file():
            config_path = probe_config
    paths = worker_sandbox.SandboxPaths(
        candidate_root=candidate_root.resolve(),
        trusted_config=config_path.resolve(),
        trusted_scripts=script_dir.resolve(),
        controller_ipc=controller_ipc,
    )
    public_env = build_public_env(candidate_root=candidate_root, probe=probe)
    if ctx is not None and ctx.lease_id:
        public_env["BRIDLE_RUN_LEASE_ID"] = ctx.lease_id
    if ctx is not None and ctx.issued_it_run_id:
        public_env["BRIDLE_IT_RUN_ID"] = ctx.issued_it_run_id
    stream_handler = None
    if ctx is not None:
        stream_handler = make_stream_handler(ctx=ctx, trusted_root=trusted_root)
    return worker_sandbox.spawn_worker(
        paths=paths,
        pytest_args=tuple(pytest_args),
        public_env=public_env,
        on_stdout_line=stream_handler,
        isolated=isolated,
    )


def finalize_controller_evidence(
    *,
    observation: Any,
    worker_stdout: str,
    trusted_root: Path,
    ctx: Any,
) -> int:
    evidence_dir = os.environ.get("BRIDLE_DOCKER_EVIDENCE_DIR", "").strip()
    if not evidence_dir or os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1":
        if observation.worker_state != "exited" or observation.exit_code is None:
            return 1
        return int(observation.exit_code)
    if observation.worker_state != "exited" or observation.exit_code is None:
        return 1
    script_dir = trusted_scripts_path(trusted_root)
    controller = _load_module("bridle_trusted_evidence_controller", script_dir / "trusted_evidence_controller.py")
    return controller.publish_from_worker_stdout(
        worker_stdout,
        trusted_scripts=script_dir,
        trusted_pythonpath=trusted_root / "backend/src",
        pytest_exitstatus=int(observation.exit_code),
        ctx=ctx,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipc-transcript", type=Path)
    parser.add_argument("--verify-overlay-after", type=Path)
    parser.add_argument("--probe-isolation", action="store_true")
    parser.add_argument("candidate_root", type=Path)
    parser.add_argument("trusted_root", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    source_env = dict(os.environ)
    os.environ.clear()
    os.environ.update(sanitized_environment(source_env))
    for key in (
        "BRIDLE_DOCKER_EVIDENCE_DIR",
        "BRIDLE_RUN_DOCKER_TESTS",
        "BRIDLE_AGENT_IMAGE",
        "BRIDLE_REVIEW_SOURCE_DIGEST",
        "BRIDLE_REVIEW_IMAGE_DIGEST",
        "GITHUB_SHA",
        "BRIDLE_WORKER_DOCKER_SANDBOX",
        "BRIDLE_FORCE_SUBPROCESS_WORKER",
        "BRIDLE_WORKER_IMAGE",
        "BRIDLE_CONTROLLER_IPC_DIR",
    ):
        value = source_env.get(key, "").strip()
        if value:
            os.environ[key] = value

    candidate_root = args.candidate_root.resolve()
    trusted_root = args.trusted_root.resolve()
    os.environ["BRIDLE_TRUSTED_CHECKOUT_ROOT"] = str(candidate_root)

    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]

    harness_path = trusted_scripts_path(trusted_root) / "trusted_harness.py"
    harness_before = harness_path.read_text(encoding="utf-8") if harness_path.exists() else ""
    before_pytest = sys.modules.get("pytest")
    evidence_path = Path(os.environ["BRIDLE_DOCKER_EVIDENCE_DIR"]) if os.environ.get("BRIDLE_DOCKER_EVIDENCE_DIR") else None
    ipc_dir = controller_ipc_dir(source_env)
    if ipc_dir is not None:
        (ipc_dir / "sentinel-acks").mkdir(parents=True, exist_ok=True)

    if args.probe_isolation:
        harness_root = trusted_root
        setup_probe_layout(candidate_root, harness_root, evidence_path)
        pytest_args = [arg for arg in pytest_args if arg.startswith("-")]

    script_dir = trusted_scripts_path(trusted_root)
    worker_sandbox = _load_module("bridle_worker_sandbox", script_dir / "worker_sandbox.py")
    evidence_controller = _load_module(
        "bridle_trusted_evidence_controller",
        script_dir / "trusted_evidence_controller.py",
    )
    controller_context = _load_module(
        "bridle_controller_context",
        script_dir / "controller_context.py",
    )

    ctx = controller_context.ControllerExecutionContext(
        candidate_root=candidate_root,
        controller_ipc_dir=ipc_dir,
    )
    isolated = None
    worker_stdout = ""
    worker_stderr = ""
    try:
        if not args.probe_isolation and os.environ.get("BRIDLE_RUN_DOCKER_TESTS") == "1" and os.name != "nt":
            evidence_controller.mark_evidence_run_started(trusted_pythonpath=trusted_root / "backend/src")
            if ipc_dir is not None:
                lease = ctx.lease_registry.create_lease(candidate_root=candidate_root, ipc_dir=ipc_dir)
                ctx.lease_id = lease.lease_id
                ctx.issued_it_run_id = uuid.uuid4().hex[:12]
                ctx.lease_registry.register_it_run_id(
                    ctx.lease_id,
                    ctx.issued_it_run_id,
                    ipc_dir=ipc_dir,
                )
                os.environ["BRIDLE_IT_RUN_ID"] = ctx.issued_it_run_id
                os.environ["BRIDLE_RUN_LEASE_ID"] = ctx.lease_id
            if worker_sandbox.use_docker_sandbox(public_env=build_public_env(candidate_root=candidate_root, probe=False)):
                emit_ci_phase("isolated_dind_start")
                isolated = worker_sandbox.start_isolated_docker_for_worker(
                    run_id=os.environ.get("GITHUB_SHA", "")[:12] or None,
                    candidate_host_root=candidate_root,
                )
                emit_ci_phase("isolated_dind_ready", detail=isolated.dind_name)
                ctx.isolated_docker_host = isolated.docker_host
                ctx.isolated_dind_name = isolated.dind_name
                ctx.isolated_network = isolated.network
                review_image = os.environ.get("BRIDLE_AGENT_IMAGE", "").strip()
                review_digest = os.environ.get("BRIDLE_REVIEW_IMAGE_DIGEST", "").strip()
                if review_image:
                    isolated_module = _load_module(
                        "bridle_isolated_docker_import",
                        script_dir / "isolated_docker.py",
                    )
                    try:
                        isolated_module.import_host_image_to_dind(
                            dind_name=isolated.dind_name,
                            image_ref=review_image,
                            expected_digest=review_digest or None,
                        )
                        emit_ci_phase("isolated_review_image_imported", detail=review_image)
                        isolated_module.verify_worker_docker_access(
                            dind_name=isolated.dind_name,
                            network=isolated.network,
                            image_ref=review_image,
                            worker_image=os.environ.get("BRIDLE_WORKER_IMAGE", "").strip()
                            or worker_sandbox.worker_image_ref(),
                            candidate_host_root=candidate_root,
                        )
                        emit_ci_phase("isolated_worker_access_verified")
                    except Exception as exc:
                        emit_ci_phase("isolated_docker_setup_failed", detail=str(exc))
                        LOGGER.error(
                            "isolated_docker_setup_failed image=%s error=%s",
                            review_image,
                            exc,
                        )
                        raise

        emit_ci_phase("worker_spawn_start")
        observation, worker_stdout, worker_stderr = run_worker(
            candidate_root=candidate_root,
            trusted_root=trusted_root,
            pytest_args=pytest_args,
            probe=args.probe_isolation,
            controller_ipc=ipc_dir,
            ctx=ctx,
            isolated=isolated,
        )

        emit_ci_phase(
            "worker_spawn_finished",
            detail=f"state={observation.worker_state} exit_code={observation.exit_code}",
        )

        worker_sandbox = _load_module("bridle_worker_sandbox", script_dir / "worker_sandbox.py")
        probe_report = worker_sandbox.parse_probe_report(worker_stdout) if args.probe_isolation else None

        if args.ipc_transcript is not None:
            partial_transcript = {
                "observation": json.loads(
                    _load_module("bridle_trusted_ipc", script_dir / "trusted_ipc.py").encode_observation(observation)
                ),
                "probe_report_untrusted": probe_report,
                "worker_stdout_sha256": hashlib.sha256(worker_stdout.encode("utf-8")).hexdigest(),
                "worker_stderr_sha256": hashlib.sha256(worker_stderr.encode("utf-8")).hexdigest(),
            }
            args.ipc_transcript.parent.mkdir(parents=True, exist_ok=True)
            args.ipc_transcript.write_text(
                json.dumps(partial_transcript, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        try:
            verify_worker_observation(observation, probe=args.probe_isolation)
            verify_controller_state(
                before_pytest=before_pytest,
                harness_before=harness_before,
                harness_path=harness_path,
                evidence_dir=evidence_path,
                probe_report=probe_report,
            )
        except RuntimeError as exc:
            LOGGER.error(
                "trusted_controller_verification_failed error=%s worker_state=%s exit_code=%s stderr_tail=%s probe_report=%s",
                exc,
                observation.worker_state,
                observation.exit_code,
                worker_stderr[-4000:],
                probe_report,
            )
            emit_worker_streams(worker_stdout, worker_stderr)
            write_controller_failure_transcript(
                error=str(exc),
                worker_stdout=worker_stdout,
                worker_stderr=worker_stderr,
                observation=observation,
            )
            raise

        if args.probe_isolation and observation.worker_uid is not None and observation.controller_uid is not None:
            if observation.worker_uid == observation.controller_uid and worker_sandbox.use_docker_sandbox(
                public_env=build_public_env(candidate_root=candidate_root, probe=True)
            ) is False:
                LOGGER.warning("worker_uid_matches_controller subprocess_mode_only")

        transcript = {
            "observation": json.loads(
                _load_module("bridle_trusted_ipc", script_dir / "trusted_ipc.py").encode_observation(observation)
            ),
            "probe_report_untrusted": probe_report,
            "worker_stdout_sha256": hashlib.sha256(worker_stdout.encode("utf-8")).hexdigest(),
            "worker_stderr_sha256": hashlib.sha256(worker_stderr.encode("utf-8")).hexdigest(),
            "verified": True,
        }
        if args.ipc_transcript is not None:
            args.ipc_transcript.write_text(json.dumps(transcript, indent=2, sort_keys=True), encoding="utf-8")

        if args.probe_isolation:
            if observation.worker_state != "exited" or observation.exit_code is None:
                if worker_stderr.strip():
                    LOGGER.error("probe_worker_stderr=%s", worker_stderr[-4000:])
                return 1
            cleanup_probe_layout(candidate_root, evidence_path)
            return 0 if observation.exit_code in {0, 5} else int(observation.exit_code)

        if observation.worker_state != "exited" or observation.exit_code is None:
            LOGGER.error(
                "docker_worker_failed state=%s exit_code=%s stderr_tail=%s stdout_tail=%s",
                observation.worker_state,
                observation.exit_code,
                worker_stderr[-8000:],
                worker_stdout[-8000:],
            )
            emit_worker_streams(worker_stdout, worker_stderr)
            write_controller_failure_transcript(
                error="docker_worker_not_exited",
                worker_stdout=worker_stdout,
                worker_stderr=worker_stderr,
                observation=observation,
            )
            return 1

        try:
            if args.verify_overlay_after is not None:
                harness = _load_module("bridle_trusted_harness", script_dir / "trusted_harness.py")
                harness.verify_overlay_snapshot(candidate_root, harness._read_snapshot(args.verify_overlay_after))
        except Exception as overlay_exc:
            emit_worker_streams(worker_stdout, worker_stderr)
            write_controller_failure_transcript(
                error=str(overlay_exc),
                worker_stdout=worker_stdout,
                worker_stderr=worker_stderr,
                observation=observation,
            )
            raise

        exit_code = finalize_controller_evidence(
            observation=observation,
            worker_stdout=worker_stdout,
            trusted_root=trusted_root,
            ctx=ctx,
        )
        if exit_code != 0:
            emit_worker_streams(worker_stdout, worker_stderr)
            write_controller_failure_transcript(
                error=f"pytest_exit_code={exit_code}",
                worker_stdout=worker_stdout,
                worker_stderr=worker_stderr,
                observation=observation,
            )
        return exit_code
    except Exception as exc:
        if worker_stdout or worker_stderr:
            emit_worker_streams(worker_stdout, worker_stderr)
        write_controller_failure_transcript(
            error=str(exc),
            worker_stdout=worker_stdout,
            worker_stderr=worker_stderr,
        )
        LOGGER.error("trusted_controller_failed error=%s", exc)
        raise
    finally:
        worker_sandbox = _load_module("bridle_worker_sandbox", script_dir / "worker_sandbox.py")
        worker_sandbox.stop_isolated_docker(isolated)


if __name__ == "__main__":
    raise SystemExit(main())

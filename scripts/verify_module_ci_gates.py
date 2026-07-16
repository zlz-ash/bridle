from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("bridle.ci.module_gates")
DIAGNOSTIC_LIMIT = 8_192
GATE_CASE_IDS = {
    "map": ("CASE-MAP-BACKEND-SUITE", "CASE-MAP-FRONTEND-SYNC"),
    "container-contract": ("CASE-CONT-CONTRACT-SUITE",),
    "container-docker-linux": ("CASE-CONT-DOCKER-LINUX",),
    "all-local": (
        "CASE-MAP-BACKEND-SUITE",
        "CASE-MAP-FRONTEND-SYNC",
        "CASE-CONT-CONTRACT-SUITE",
    ),
}
KNOWN_CASE_IDS = {
    "CASE-MAP-BACKEND-SUITE",
    "CASE-MAP-FRONTEND-SYNC",
    "CASE-MAP-OBSERVABILITY-RED",
    "CASE-CONT-CONTRACT-SUITE",
    "CASE-CONT-OBSERVABILITY-RED",
    "CASE-CONT-DOCKER-LINUX",
    "CASE-CI-VERIFIER-CONTRACT",
}
_SECRET = re.compile(r"(?i)\b(token|password|secret|authorization)\s*[:=]\s*[^\s]+")
_SHELL_TOKENS = ("&&", "||", "|", ">", "<", "`", "$(")


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ContractError(ValueError):
    pass


def contract_fingerprint(contract: dict[str, Any]) -> str:
    serialized = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if contract.get("schema_version") != 1:
        errors.append("schema_version_invalid")
    cases = contract.get("cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["cases_missing"]
    seen: set[str] = set()
    for row in cases:
        if not isinstance(row, dict):
            errors.append("case_invalid")
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str):
            errors.append("case_id_missing")
            continue
        if case_id in seen:
            errors.append(f"duplicate_case_id:{case_id}")
        seen.add(case_id)
        if case_id not in KNOWN_CASE_IDS:
            errors.append(f"unknown_case_id:{case_id}")
        if not isinstance(row.get("command"), str) or not row["command"].strip():
            errors.append(f"missing_command:{case_id}")
        if not isinstance(row.get("timeout_seconds"), int) or row["timeout_seconds"] <= 0:
            errors.append(f"invalid_timeout:{case_id}")
    return errors


def select_cases(contract: dict[str, Any], gate: str) -> list[dict[str, Any]]:
    if gate not in GATE_CASE_IDS:
        raise ContractError(f"unknown_gate:{gate}")
    by_id = {
        row["case_id"]: row
        for row in contract.get("cases", [])
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }
    missing = [case_id for case_id in GATE_CASE_IDS[gate] if case_id not in by_id]
    if missing:
        raise ContractError(f"selected_cases_missing:{','.join(missing)}")
    return [by_id[case_id] for case_id in GATE_CASE_IDS[gate]]


def _parse_command(command: str, project_root: Path) -> tuple[list[str], Path]:
    try:
        resolved_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise ContractError("unsafe_working_directory") from exc
    cwd = resolved_root
    executable = command.strip()
    if ";" in executable:
        prefix, executable = (part.strip() for part in executable.split(";", 1))
        if not prefix.startswith("cd "):
            raise ContractError("unsupported_command_prefix")
        relative = prefix[3:].strip().replace("\\", "/")
        if not relative or relative.startswith("/") or ":" in relative or ".." in relative.split("/"):
            raise ContractError("unsafe_working_directory")
        candidate_cwd = resolved_root.joinpath(*relative.split("/"))
        try:
            cwd = candidate_cwd.resolve(strict=True)
            cwd.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise ContractError("unsafe_working_directory") from exc
    if any(token in executable for token in _SHELL_TOKENS):
        raise ContractError("shell_operator_not_allowed")
    argv = shlex.split(executable, posix=True)
    if not argv:
        raise ContractError("empty_argv")
    return argv, cwd


def resolve_executable(argv: list[str], project_root: Path, *, cwd: Path | None = None) -> list[str]:
    resolved = list(argv)
    if resolved[0] == "python":
        bases = [base for base in (cwd, project_root) if base is not None]
        for base in bases:
            scripts_dir = base / ".venv" / ("Scripts" if os.name == "nt" else "bin")
            candidate = scripts_dir / ("python.exe" if os.name == "nt" else "python")
            if candidate.is_file():
                resolved[0] = str(candidate.resolve())
                break
    elif os.name == "nt" and resolved[0] in {"npm", "npx"}:
        shim = shutil.which(f"{resolved[0]}.cmd")
        if shim:
            resolved[0] = shim
    return resolved


def _default_runner(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _diagnostics(stdout: str, stderr: str) -> str:
    redacted = _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", f"{stdout}\n{stderr}")
    return redacted[-DIAGNOSTIC_LIMIT:]


def _required_test_results(stdout: str, required_symbols: list[str]) -> tuple[list[str], list[str]]:
    passed_nodes: dict[str, list[str]] = {symbol: [] for symbol in required_symbols}
    for line in stdout.splitlines():
        stripped = line.strip()
        pytest_match = re.fullmatch(r"(?P<node>.+\.py::.+?)\s+PASSED\s+\[\s*\d+%\]", stripped)
        vitest_match = re.fullmatch(
            r"[✓√]\s+(?P<node>\S+\.(?:test|spec)\.[cm]?[jt]sx?(?:\s+>.+){2,}?)(?:\s+\d+(?:\.\d+)?ms)?",
            stripped,
        )
        if pytest_match:
            node_id = pytest_match.group("node")
            terminal_identity = node_id.rsplit("::", 1)[-1]
            identity_kind = "pytest"
        elif vitest_match:
            node_id = vitest_match.group("node")
            terminal_identity = re.sub(r"\s+\d+(?:\.\d+)?ms$", "", node_id.rsplit(" > ", 1)[-1])
            identity_kind = "vitest"
        else:
            continue
        for symbol in required_symbols:
            comparable_identity = terminal_identity
            if identity_kind == "pytest" and "[" not in symbol:
                comparable_identity = terminal_identity.split("[", 1)[0]
            if symbol == comparable_identity:
                passed_nodes[symbol].append(node_id)
    missing = [symbol for symbol, nodes in passed_nodes.items() if not nodes]
    duplicates = [symbol for symbol, nodes in passed_nodes.items() if len(nodes) > 1]
    return missing, duplicates


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_gate(
    contract_path: Path,
    *,
    gate: str,
    project_root: Path,
    output_dir: Path,
    runner: Runner = _default_runner,
) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    errors = validate_contract(contract)
    if errors:
        raise ContractError(";".join(errors))
    if gate == "container-docker-linux":
        raise ContractError("trusted_linux_docker_workflow_required")
    selected = select_cases(contract, gate)
    output_dir.mkdir(parents=True, exist_ok=True)
    events_path = output_dir / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    events: list[dict[str, Any]] = []
    LOGGER.info("module_gate_started gate=%s cases=%d", gate, len(selected))
    for case in selected:
        case_id = str(case["case_id"])
        argv, cwd = _parse_command(str(case["command"]), project_root)
        if runner is _default_runner:
            argv = resolve_executable(argv, project_root, cwd=cwd)
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        error_code: str | None = None
        stdout = ""
        stderr = ""
        try:
            completed = runner(argv, cwd=cwd, timeout=int(case["timeout_seconds"]))
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            error_code = "command_timeout"
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "")
        except FileNotFoundError as exc:
            exit_code = 127
            error_code = "executable_missing"
            stderr = str(exc)
        required_symbols = [
            str(symbol)
            for symbol in case.get("required_test_symbols", [])
            if isinstance(symbol, str) and symbol
        ]
        missing_symbols, duplicate_symbols = _required_test_results(stdout, required_symbols)
        if exit_code == 0 and duplicate_symbols:
            exit_code = 4
            error_code = "required_test_identity_duplicate"
        elif exit_code == 0 and missing_symbols:
            exit_code = 3
            error_code = "required_tests_not_observed"
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        event = {
            "case_id": case_id,
            "stage": "test",
            "argv": argv,
            "cwd": cwd.as_posix(),
            "started_at": started_at,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "status": "passed" if exit_code == 0 else "failed",
            "error_code": error_code,
            "missing_test_symbols": missing_symbols,
            "duplicate_test_symbols": duplicate_symbols,
            "diagnostics": _diagnostics(stdout, stderr),
        }
        events.append(event)
        with events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        LOGGER.info(
            "module_gate_case_finished case_id=%s exit_code=%d duration_ms=%d",
            case_id,
            exit_code,
            duration_ms,
        )
    first_failure = next((event for event in events if event["exit_code"] != 0), None)
    summary = {
        "schema_version": 1,
        "batch_id": contract["batch_id"],
        "gate": gate,
        "case_ids": [event["case_id"] for event in events],
        "contract_fingerprint": contract_fingerprint(contract),
        "status": "failed" if first_failure else "passed",
        "exit_code": first_failure["exit_code"] if first_failure else 0,
        "events_path": events_path.as_posix(),
    }
    _write_json(output_dir / "summary.json", summary)
    LOGGER.info("module_gate_finished gate=%s status=%s", gate, summary["status"])
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run reviewed project-map and container CI contracts.")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--gate", choices=sorted(GATE_CASE_IDS), default="all-local")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        errors = validate_contract(contract)
        if errors:
            raise ContractError(";".join(errors))
        if args.list:
            selected = select_cases(contract, args.gate)
            payload = {
                "gate": args.gate,
                "case_ids": [case["case_id"] for case in selected],
                "contract_fingerprint": contract_fingerprint(contract),
            }
            print(json.dumps(payload, sort_keys=True) if args.format == "json" else "\n".join(payload["case_ids"]))
            return 0
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_dir = args.output_dir or args.project_root / ".ai-dev" / "ci" / "runs" / run_id
        summary = run_gate(
            args.contract,
            gate=args.gate,
            project_root=args.project_root.resolve(),
            output_dir=output_dir,
        )
        print(json.dumps(summary, sort_keys=True) if args.format == "json" else summary["status"])
        return int(summary["exit_code"])
    except (ContractError, json.JSONDecodeError, OSError) as exc:
        LOGGER.error("module_gate_failed error=%s", _diagnostics("", str(exc)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

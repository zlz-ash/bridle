"""Tests for the controlled agent container entrypoint."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bridle.agent.container import entrypoint
from bridle.agent.container.container_control import (
    CONTROL_ENVELOPE_PREFIX,
    parse_control_envelope_from_exec_output,
)
from bridle.agent.container.entrypoint import run_container_task


def _candidate_layout(module_root, candidate_id: str = "cand-1"):
    candidate_rel = f"candidates/{candidate_id}"
    candidate_root = module_root / candidate_rel
    for sub in ("project", "baseline", "output", "diagnostics"):
        (candidate_root / sub).mkdir(parents=True, exist_ok=True)
    return candidate_rel, candidate_root


def _write_request(candidate_root: Path, commands: list[dict], write_set: list[str] | None = None) -> None:
    payload = {
        "schema": "bridle.container_test_request/v1",
        "commands": commands,
        "write_set": write_set or [],
    }
    (candidate_root / "diagnostics" / "test-request.json").write_text(json.dumps(payload), encoding="utf-8")


class TestContainerEntrypoint:
    def test_runs_approved_commands_and_writes_output(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        ok_cmd = {
            "command_id": "cmd-1",
            "argv": [sys.executable, "-c", "print('ok')"],
            "raw_command": "python -c \"print('ok')\"",
        }
        _write_request(candidate_root, [ok_cmd], write_set=[])

        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 0
        assert manifest["status"] == "completed"
        assert manifest["schema"] == "bridle.container_test_result/v1"
        assert manifest["results"][0]["exit_code"] == 0
        assert "ok" in manifest["results"][0]["stdout"]

    def test_failed_test_returns_nonzero_manifest(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        _write_request(
            candidate_root,
            [{"command_id": "cmd-1", "argv": [sys.executable, "-c", "raise SystemExit(7)"], "raw_command": "x"}],
        )

        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 7
        assert manifest["status"] == "failed"
        assert manifest["error_code"] == "test_failed"

    def test_timeout_output_is_serializable(self, tmp_path, monkeypatch) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        _write_request(candidate_root, [{"command_id": "cmd-1", "argv": ["slow"], "raw_command": "slow"}])

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="slow", timeout=1, output=b"partial output")

        monkeypatch.setattr(entrypoint.subprocess, "run", fake_run)
        exit_code = run_container_task(module_root, candidate_rel=candidate_rel, timeout_seconds=1)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == -1
        assert manifest["status"] == "failed"
        assert manifest["results"][0]["stdout"] == "partial output"
        assert manifest["results"][0]["timed_out"] is True

    def test_out_of_scope_change_detected(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        project = candidate_root / "project"
        baseline = candidate_root / "baseline"
        (baseline / "src").mkdir(parents=True)
        (baseline / "src" / "a.py").write_text("base\n", encoding="utf-8")
        (project / "src").mkdir(parents=True)
        (project / "src" / "a.py").write_text("changed\n", encoding="utf-8")
        _write_request(
            candidate_root,
            [
                {
                    "command_id": "cmd-1",
                    "argv": [sys.executable, "-c", "open('extra.py','w',encoding='utf-8').write('x\\n')"],
                    "raw_command": "python -c write extra",
                }
            ],
            write_set=["src/a.py"],
        )

        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 4
        assert manifest["error_code"] == "out_of_scope_change"
        assert "extra.py" in manifest["out_of_scope_changes"]

    def test_baseline_only_paths_do_not_trigger_out_of_scope(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        (candidate_root / "baseline" / "tests").mkdir(parents=True)
        (candidate_root / "baseline" / "tests" / "test_ok.py").write_text(
            "def test_ok(): assert True\n", encoding="utf-8"
        )
        _write_request(
            candidate_root,
            [{"command_id": "cmd-1", "argv": [sys.executable, "-c", "print(1)"], "raw_command": "x"}],
            write_set=[],
        )

        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 0
        assert manifest["status"] == "completed"
        assert manifest["out_of_scope_changes"] == []

    def test_pytest_cache_is_ephemeral(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        tests = candidate_root / "project" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        _write_request(
            candidate_root,
            [
                {
                    "command_id": "cmd-1",
                    "argv": [sys.executable, "-m", "pytest", "tests/test_ok.py", "-q"],
                    "raw_command": "python -m pytest tests/test_ok.py -q",
                }
            ],
            write_set=["tests/test_ok.py"],
        )

        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 0, manifest
        assert manifest["status"] == "completed"
        assert manifest["out_of_scope_changes"] == []

    def test_unknown_schema_fails_closed(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        (candidate_root / "diagnostics" / "test-request.json").write_text(
            json.dumps({"schema": "bad/v9", "commands": []}),
            encoding="utf-8",
        )
        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 2
        assert manifest["error_code"] == "unknown_test_request_schema"

    def test_legacy_tests_json_is_not_executed(self, tmp_path) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        legacy = candidate_root / "tests"
        legacy.mkdir()
        cmd = f'{sys.executable} -c "print(\'legacy\')"'
        (legacy / "tests.json").write_text(json.dumps({"tests": [cmd]}), encoding="utf-8")
        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        manifest = json.loads((candidate_root / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 2
        assert manifest["error_code"] == "missing_test_request_manifest"

    @pytest.mark.parametrize(
        "candidate_rel",
        ["", "../other", "/abs/path", "candidates/../x", "candidates/a\\b", "outside/cand-1"],
    )
    def test_rejects_invalid_candidate_rel_before_fs_ops(self, tmp_path, candidate_rel: str) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        sentinel = tmp_path / "outside-sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        assert exit_code == 2
        assert sentinel.read_text(encoding="utf-8") == "keep\n"

    def test_emits_control_envelope_when_output_unwritable(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        monkeypatch.setenv("BRIDLE_RUN_ID", "run-envelope")
        ok_cmd = {
            "command_id": "cmd-1",
            "argv": [sys.executable, "-c", "print('ok')"],
            "raw_command": "python -c \"print('ok')\"",
        }
        _write_request(candidate_root, [ok_cmd], write_set=[])

        def _deny_manifest_write(output_dir: Path, manifest: dict) -> None:
            raise OSError("simulated output permission denied")

        monkeypatch.setattr(entrypoint, "_write_manifest", _deny_manifest_write)
        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        captured = capsys.readouterr()
        assert exit_code == 0
        assert CONTROL_ENVELOPE_PREFIX in captured.out
        envelope = parse_control_envelope_from_exec_output(
            captured.out,
            expected_run_id="run-envelope",
            expected_candidate_rel=candidate_rel,
        )
        assert envelope["manifest"]["status"] == "completed"
        assert envelope["manifest"]["results"][0]["stdout"] == "ok\n"

    def test_control_envelope_survives_replacement_character_in_output(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        module_root = tmp_path / "module"
        module_root.mkdir()
        candidate_rel, candidate_root = _candidate_layout(module_root)
        monkeypatch.setenv("BRIDLE_RUN_ID", "run-unicode")
        unicode_cmd = {
            "command_id": "cmd-1",
            "argv": [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write('\\ufffd中文\\n'.encode('utf-8')); sys.stdout.buffer.flush()",
            ],
            "raw_command": "python -c unicode",
        }
        _write_request(candidate_root, [unicode_cmd], write_set=[])
        exit_code = run_container_task(module_root, candidate_rel=candidate_rel)
        captured = capsys.readouterr()
        assert exit_code == 0
        envelope_line = next(
            line for line in captured.out.splitlines() if line.startswith(CONTROL_ENVELOPE_PREFIX)
        )
        assert envelope_line.isascii()
        envelope = parse_control_envelope_from_exec_output(
            captured.out,
            expected_run_id="run-unicode",
            expected_candidate_rel=candidate_rel,
        )
        assert envelope["manifest"]["status"] == "completed"
        assert "\ufffd" in envelope["manifest"]["results"][0]["stdout"]

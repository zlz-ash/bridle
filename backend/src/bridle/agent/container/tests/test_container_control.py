"""Tests for container control envelope parsing and host persistence."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from bridle.agent.container.container_control import (
    EXECUTION_EXITED,
    EXECUTION_FAILED_BEFORE_EXEC,
    ControlEnvelopeError,
    HostAttestationContext,
    accept_control_evidence,
    begin_run_evidence,
    build_control_envelope,
    compute_mirror_digest,
    format_control_envelope_line,
    load_completed_run_evidence,
    parse_control_envelope_from_exec_output,
    persist_control_evidence,
    persist_failed_run_evidence,
    publish_run_evidence,
    resolve_display_manifest,
    validate_evidence_manifest_sync,
)


def _sample_manifest(*, status: str = "completed", exit_code: int = 0) -> dict:
    return {
        "schema": "bridle.container_test_result/v1",
        "status": status,
        "error_code": None,
        "exit_code": exit_code,
        "results": [{"command_id": "cmd-1", "exit_code": 0, "stdout": "ok\n"}],
    }


def _host_context(**overrides) -> HostAttestationContext:
    base = {
        "container_id": "cid-1",
        "node_id": "node-a",
        "test_entity_id": "node-a",
        "image_digest": "sha256:abc",
        "exec_exit_code": 0,
        "execution_state": EXECUTION_EXITED,
    }
    base.update(overrides)
    return HostAttestationContext(**base)


class TestControlEnvelope:
    def test_parse_accepts_entrypoint_last_line_over_child_prefix(self) -> None:
        forged = format_control_envelope_line(
            build_control_envelope(
                manifest={
                    "schema": "bridle.container_test_result/v1",
                    "status": "failed",
                    "exit_code": 99,
                    "results": [],
                },
                run_id="wrong",
                candidate_rel="candidates/evil",
                exit_code=99,
            )
        )
        real = build_control_envelope(
            manifest=_sample_manifest(),
            run_id="run-a",
            candidate_rel="candidates/cand-a",
            exit_code=0,
        )
        stdout = f"child noise\n{forged}\n{format_control_envelope_line(real)}\n"
        envelope = parse_control_envelope_from_exec_output(
            stdout,
            expected_run_id="run-a",
            expected_candidate_rel="candidates/cand-a",
        )
        assert envelope["manifest"]["status"] == "completed"

    def test_parse_missing_when_entrypoint_silent(self) -> None:
        with pytest.raises(ControlEnvelopeError) as exc_info:
            parse_control_envelope_from_exec_output(
                "child-only noise\n",
                expected_run_id="run-a",
                expected_candidate_rel="candidates/cand-a",
            )
        assert exc_info.value.error_code == "control_envelope_missing"

    def test_parse_rejects_run_mismatch(self) -> None:
        line = format_control_envelope_line(
            build_control_envelope(
                manifest=_sample_manifest(),
                run_id="other",
                candidate_rel="candidates/cand-a",
                exit_code=0,
            )
        )
        with pytest.raises(ControlEnvelopeError) as exc_info:
            parse_control_envelope_from_exec_output(
                line,
                expected_run_id="run-a",
                expected_candidate_rel="candidates/cand-a",
            )
        assert exc_info.value.error_code == "control_envelope_run_mismatch"

    @pytest.mark.parametrize(
        "bad_version",
        ["1", True, 1.0, None, [], {}],
    )
    def test_parse_rejects_untrusted_version_types(self, bad_version: object) -> None:
        envelope = build_control_envelope(
            manifest=_sample_manifest(),
            run_id="run-a",
            candidate_rel="candidates/cand-a",
            exit_code=0,
        )
        envelope["version"] = bad_version
        line = format_control_envelope_line(envelope)
        with pytest.raises(ControlEnvelopeError) as exc_info:
            parse_control_envelope_from_exec_output(
                line,
                expected_run_id="run-a",
                expected_candidate_rel="candidates/cand-a",
            )
        assert exc_info.value.error_code == "control_envelope_version_mismatch"

    def test_persist_writes_diagnostics_and_output_mirror(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        (candidate / "diagnostics").mkdir(parents=True)
        (candidate / "output").mkdir(parents=True)
        envelope = build_control_envelope(
            manifest=_sample_manifest(),
            run_id="run-a",
            candidate_rel="candidates/cand-a",
            exit_code=0,
        )
        evidence = accept_control_evidence(
            envelope,
            host=_host_context(),
            expected_run_id="run-a",
            expected_candidate_rel="candidates/cand-a",
            expected_node_id="node-a",
            expected_test_entity_id="node-a",
            expected_container_id="cid-1",
        )
        persist_control_evidence(candidate, evidence)
        stored = json.loads((candidate / "diagnostics" / "control-envelope.json").read_text(encoding="utf-8"))
        assert stored["host_attestation"]["container_id"] == "cid-1"
        manifest = json.loads((candidate / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"


class TestHostAttestationBinding:
    @staticmethod
    def _mutate_host(host: HostAttestationContext, **changes) -> HostAttestationContext:
        data = {
            "container_id": host.container_id,
            "node_id": host.node_id,
            "test_entity_id": host.test_entity_id,
            "image_digest": host.image_digest,
            "exec_exit_code": host.exec_exit_code,
        }
        data.update(changes)
        return HostAttestationContext(**data)

    @pytest.mark.parametrize(
        ("field", "value", "expected_code"),
        [
            ("container_id", "other", "control_evidence_container_mismatch"),
            ("node_id", "wrong", "control_evidence_node_mismatch"),
            ("test_entity_id", "wrong", "control_evidence_test_entity_mismatch"),
            ("exec_exit_code", 9, "control_evidence_exec_exit_mismatch"),
        ],
    )
    def test_accept_rejects_host_tampering(
        self,
        field: str,
        value: object,
        expected_code: str,
    ) -> None:
        envelope = build_control_envelope(
            manifest=_sample_manifest(exit_code=0),
            run_id="run-a",
            candidate_rel="candidates/cand-a",
            exit_code=0,
        )
        host = _host_context()
        bad_host = self._mutate_host(host, **{field: value})
        with pytest.raises(ControlEnvelopeError) as exc_info:
            accept_control_evidence(
                envelope,
                host=bad_host,
                expected_run_id="run-a",
                expected_candidate_rel="candidates/cand-a",
                expected_node_id="node-a",
                expected_test_entity_id="node-a",
                expected_container_id="cid-1",
            )
        assert exc_info.value.error_code == expected_code

    def test_accept_rejects_manifest_exit_mismatch(self) -> None:
        envelope = build_control_envelope(
            manifest=_sample_manifest(exit_code=5),
            run_id="run-a",
            candidate_rel="candidates/cand-a",
            exit_code=0,
        )
        with pytest.raises(ControlEnvelopeError) as exc_info:
            accept_control_evidence(
                envelope,
                host=_host_context(exec_exit_code=0),
                expected_run_id="run-a",
                expected_candidate_rel="candidates/cand-a",
                expected_node_id="node-a",
                expected_test_entity_id="node-a",
                expected_container_id="cid-1",
            )
        assert exc_info.value.error_code == "control_envelope_manifest_exit_mismatch"


class TestManifestExitRequired:
    def test_validate_rejects_missing_manifest_exit_code(self) -> None:
        envelope = build_control_envelope(
            manifest={
                "schema": "bridle.container_test_result/v1",
                "status": "completed",
                "results": [],
            },
            run_id="run-a",
            candidate_rel="candidates/cand-a",
            exit_code=0,
        )
        with pytest.raises(ControlEnvelopeError) as exc_info:
            parse_control_envelope_from_exec_output(
                format_control_envelope_line(envelope),
                expected_run_id="run-a",
                expected_candidate_rel="candidates/cand-a",
            )
        assert exc_info.value.error_code == "control_envelope_manifest_exit_missing"


class TestRunEvidenceLifecycle:
    def test_failed_run_does_not_expose_prior_completed_evidence(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        (candidate / "diagnostics").mkdir(parents=True)
        (candidate / "output").mkdir(parents=True)
        success = accept_control_evidence(
            build_control_envelope(
                manifest=_sample_manifest(exit_code=0),
                run_id="run-success",
                candidate_rel="candidates/cand-a",
                exit_code=0,
            ),
            host=_host_context(),
            expected_run_id="run-success",
            expected_candidate_rel="candidates/cand-a",
            expected_node_id="node-a",
            expected_test_entity_id="node-a",
            expected_container_id="cid-1",
        )
        persist_control_evidence(candidate, success)
        begin_run_evidence(
            candidate,
            run_id="run-fail",
            candidate_rel="candidates/cand-a",
            node_id="node-a",
            test_entity_id="node-a",
            image_digest="sha256:abc",
        )
        manifest = json.loads((candidate / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "pending"
        assert manifest["run_id"] == "run-fail"
        with pytest.raises(ControlEnvelopeError) as exc_info:
            load_completed_run_evidence(candidate, expected_run_id="run-success")
        assert exc_info.value.error_code == "control_evidence_run_mismatch"
        with pytest.raises(ControlEnvelopeError) as exc_info:
            load_completed_run_evidence(candidate, expected_run_id="run-fail")
        assert exc_info.value.error_code == "control_evidence_not_completed"


def _persist_completed_success(candidate: Path, *, run_id: str = "run-success") -> None:
    success = accept_control_evidence(
        build_control_envelope(
            manifest=_sample_manifest(exit_code=0),
            run_id=run_id,
            candidate_rel="candidates/cand-a",
            exit_code=0,
        ),
        host=_host_context(),
        expected_run_id=run_id,
        expected_candidate_rel="candidates/cand-a",
        expected_node_id="node-a",
        expected_test_entity_id="node-a",
        expected_container_id="cid-1",
    )
    persist_control_evidence(candidate, success)


def _flaky_second_write(monkeypatch: pytest.MonkeyPatch) -> None:
    original_atomic = publish_run_evidence.__globals__["_atomic_write_json"]
    calls = {"count": 0}

    def flaky_atomic(path: Path, payload: dict) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated mirror write failure")
        original_atomic(path, payload)

    monkeypatch.setattr(
        "bridle.agent.container.container_control._atomic_write_json",
        flaky_atomic,
    )


class TestEvidenceTransaction:
    @pytest.mark.parametrize(
        "publish_action",
        [
            lambda candidate: begin_run_evidence(
                candidate,
                run_id="run-next",
                candidate_rel="candidates/cand-a",
                node_id="node-a",
                test_entity_id="node-a",
                image_digest="sha256:abc",
            ),
            lambda candidate: persist_control_evidence(
                candidate,
                accept_control_evidence(
                    build_control_envelope(
                        manifest=_sample_manifest(exit_code=0),
                        run_id="run-next",
                        candidate_rel="candidates/cand-a",
                        exit_code=0,
                    ),
                    host=_host_context(),
                    expected_run_id="run-next",
                    expected_candidate_rel="candidates/cand-a",
                    expected_node_id="node-a",
                    expected_test_entity_id="node-a",
                    expected_container_id="cid-1",
                ),
            ),
            lambda candidate: persist_failed_run_evidence(
                candidate,
                run_id="run-next",
                candidate_rel="candidates/cand-a",
                error_code="container_start_failed",
                node_id="node-a",
                test_entity_id="node-a",
                image_digest="sha256:abc",
                execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                exec_exit_code=None,
            ),
        ],
        ids=["begin", "persist_control_evidence", "persist_failed_run_evidence"],
    )
    def test_half_commit_after_authoritative_fails_closed_on_old_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        publish_action: Callable[[Path], None],
    ) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        (candidate / "diagnostics").mkdir(parents=True)
        (candidate / "output").mkdir(parents=True)
        _persist_completed_success(candidate)

        _flaky_second_write(monkeypatch)
        with pytest.raises(OSError):
            publish_action(candidate)

        with pytest.raises(ControlEnvelopeError) as exc_info:
            resolve_display_manifest(candidate, expected_run_id="run-success")
        assert exc_info.value.error_code in {
            "control_evidence_run_mismatch",
            "control_evidence_not_completed",
            "control_evidence_mirror_binding_mismatch",
            "control_evidence_mirror_digest_mismatch",
        }
        with pytest.raises(ControlEnvelopeError):
            load_completed_run_evidence(candidate, expected_run_id="run-success")

    def test_new_completed_authoritative_with_stale_completed_mirror_rejected(
        self, tmp_path: Path
    ) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        (candidate / "diagnostics").mkdir(parents=True)
        (candidate / "output").mkdir(parents=True)
        _persist_completed_success(candidate, run_id="run-old")
        stale_mirror = json.loads(
            (candidate / "output" / "manifest.json").read_text(encoding="utf-8")
        )

        new_success = accept_control_evidence(
            build_control_envelope(
                manifest=_sample_manifest(exit_code=0),
                run_id="run-new",
                candidate_rel="candidates/cand-a",
                exit_code=0,
            ),
            host=_host_context(),
            expected_run_id="run-new",
            expected_candidate_rel="candidates/cand-a",
            expected_node_id="node-a",
            expected_test_entity_id="node-a",
            expected_container_id="cid-1",
        )
        publish_run_evidence(
            candidate,
            authoritative=new_success,
            manifest_mirror=new_success["envelope"]["manifest"],
        )
        stale_mirror["evidence_run_id"] = "run-old"
        stale_mirror["evidence_status"] = "completed"
        (candidate / "output" / "manifest.json").write_text(
            json.dumps(stale_mirror, indent=2),
            encoding="utf-8",
        )

        with pytest.raises(ControlEnvelopeError) as exc_info:
            validate_evidence_manifest_sync(candidate)
        assert exc_info.value.error_code == "control_evidence_mirror_binding_mismatch"
        with pytest.raises(ControlEnvelopeError):
            resolve_display_manifest(candidate, expected_run_id="run-new")

    @pytest.mark.parametrize(
        ("mutator", "expected_code"),
        [
            (lambda mirror: mirror.pop("evidence_run_id", None), "control_evidence_mirror_binding_missing"),
            (lambda mirror: mirror.pop("evidence_status", None), "control_evidence_mirror_binding_missing"),
            (
                lambda mirror: mirror.update({"evidence_run_id": "wrong-run"}),
                "control_evidence_mirror_binding_mismatch",
            ),
            (
                lambda mirror: mirror.update({"evidence_status": "failed"}),
                "control_evidence_mirror_binding_mismatch",
            ),
        ],
        ids=["missing_run_id", "missing_status", "wrong_run_id", "wrong_status"],
    )
    def test_mirror_binding_fields_enforced(
        self,
        tmp_path: Path,
        mutator: Callable[[dict], None],
        expected_code: str,
    ) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        _persist_completed_success(candidate)
        mirror_path = candidate / "output" / "manifest.json"
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
        mutator(mirror)
        mirror_path.write_text(json.dumps(mirror, indent=2), encoding="utf-8")

        with pytest.raises(ControlEnvelopeError) as exc_info:
            validate_evidence_manifest_sync(candidate)
        assert exc_info.value.error_code == expected_code

    def test_mirror_digest_mismatch_rejected(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        _persist_completed_success(candidate)
        mirror_path = candidate / "output" / "manifest.json"
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
        mirror["results"][0]["stdout"] = "tampered\n"
        mirror_path.write_text(json.dumps(mirror, indent=2), encoding="utf-8")

        with pytest.raises(ControlEnvelopeError) as exc_info:
            validate_evidence_manifest_sync(candidate)
        assert exc_info.value.error_code == "control_evidence_mirror_digest_mismatch"

    def test_valid_mirror_passes_binding_and_digest(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        _persist_completed_success(candidate)
        validate_evidence_manifest_sync(candidate)
        manifest = resolve_display_manifest(candidate, expected_run_id="run-success")
        assert manifest["status"] == "completed"
        authoritative = json.loads(
            (candidate / "diagnostics" / "control-envelope.json").read_text(encoding="utf-8")
        )
        mirror = json.loads(
            (candidate / "output" / "manifest.json").read_text(encoding="utf-8")
        )
        assert mirror["evidence_run_id"] == authoritative["run_id"]
        assert mirror["evidence_status"] == authoritative["status"]
        assert authoritative["mirror_digest"] == compute_mirror_digest(mirror)

    @pytest.mark.parametrize(
        "publish_action",
        [
            lambda candidate: begin_run_evidence(
                candidate,
                run_id="run-pending",
                candidate_rel="candidates/cand-a",
                node_id="node-a",
                test_entity_id="node-a",
                image_digest="sha256:abc",
            ),
            lambda candidate: persist_control_evidence(
                candidate,
                accept_control_evidence(
                    build_control_envelope(
                        manifest=_sample_manifest(exit_code=0),
                        run_id="run-completed",
                        candidate_rel="candidates/cand-a",
                        exit_code=0,
                    ),
                    host=_host_context(),
                    expected_run_id="run-completed",
                    expected_candidate_rel="candidates/cand-a",
                    expected_node_id="node-a",
                    expected_test_entity_id="node-a",
                    expected_container_id="cid-1",
                ),
            ),
            lambda candidate: persist_failed_run_evidence(
                candidate,
                run_id="run-failed",
                candidate_rel="candidates/cand-a",
                error_code="container_start_failed",
                node_id="node-a",
                test_entity_id="node-a",
                image_digest="sha256:abc",
                execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                exec_exit_code=None,
            ),
        ],
        ids=["begin", "persist_control_evidence", "persist_failed_run_evidence"],
    )
    def test_fresh_candidate_half_commit_rejects_missing_mirror(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        publish_action: Callable[[Path], None],
    ) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        (candidate / "diagnostics").mkdir(parents=True)
        (candidate / "output").mkdir(parents=True)
        assert not (candidate / "output" / "manifest.json").exists()

        _flaky_second_write(monkeypatch)
        with pytest.raises(OSError):
            publish_action(candidate)

        assert (candidate / "diagnostics" / "control-envelope.json").is_file()
        assert not (candidate / "output" / "manifest.json").exists()

        with pytest.raises(ControlEnvelopeError) as exc_info:
            validate_evidence_manifest_sync(candidate)
        assert exc_info.value.error_code == "control_evidence_mirror_missing"

        authoritative = json.loads(
            (candidate / "diagnostics" / "control-envelope.json").read_text(encoding="utf-8")
        )
        run_id = str(authoritative["run_id"])
        with pytest.raises(ControlEnvelopeError):
            resolve_display_manifest(candidate, expected_run_id=run_id)
        if authoritative.get("status") == "completed":
            with pytest.raises(ControlEnvelopeError) as exc_info:
                load_completed_run_evidence(candidate, expected_run_id=run_id)
            assert exc_info.value.error_code == "control_evidence_mirror_missing"

    @pytest.mark.parametrize(
        ("mutator", "expected_code"),
        [
            (lambda auth: auth.pop("mirror_digest", None), "control_evidence_mirror_digest_missing"),
            (lambda auth: auth.update({"mirror_digest": None}), "control_evidence_mirror_digest_missing"),
            (lambda auth: auth.update({"mirror_digest": ""}), "control_evidence_mirror_digest_missing"),
            (lambda auth: auth.update({"mirror_digest": "not-a-digest"}), "control_evidence_mirror_digest_invalid"),
            (
                lambda auth: auth.update({"mirror_digest": "a" * 64}),
                "control_evidence_mirror_digest_mismatch",
            ),
        ],
        ids=["missing", "null", "empty", "invalid_format", "wrong_digest"],
    )
    def test_authoritative_mirror_digest_required(
        self,
        tmp_path: Path,
        mutator: Callable[[dict], None],
        expected_code: str,
    ) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        _persist_completed_success(candidate)
        auth_path = candidate / "diagnostics" / "control-envelope.json"
        authoritative = json.loads(auth_path.read_text(encoding="utf-8"))
        mutator(authoritative)
        auth_path.write_text(json.dumps(authoritative, indent=2), encoding="utf-8")

        with pytest.raises(ControlEnvelopeError) as exc_info:
            validate_evidence_manifest_sync(candidate)
        assert exc_info.value.error_code == expected_code

    def test_binding_correct_but_digest_missing_still_rejected(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        _persist_completed_success(candidate)
        auth_path = candidate / "diagnostics" / "control-envelope.json"
        authoritative = json.loads(auth_path.read_text(encoding="utf-8"))
        mirror = json.loads(
            (candidate / "output" / "manifest.json").read_text(encoding="utf-8")
        )
        assert mirror["evidence_run_id"] == authoritative["run_id"]
        assert mirror["evidence_status"] == authoritative["status"]
        authoritative.pop("mirror_digest")
        auth_path.write_text(json.dumps(authoritative, indent=2), encoding="utf-8")

        with pytest.raises(ControlEnvelopeError) as exc_info:
            validate_evidence_manifest_sync(candidate)
        assert exc_info.value.error_code == "control_evidence_mirror_digest_missing"

    def test_failed_before_exec_does_not_fabricate_exit_code(self, tmp_path: Path) -> None:
        candidate = tmp_path / "candidates" / "cand-a"
        persist_failed_run_evidence(
            candidate,
            run_id="run-no-exec",
            candidate_rel="candidates/cand-a",
            error_code="container_start_failed",
            node_id="node-a",
            test_entity_id="node-a",
            image_digest="sha256:abc",
            execution_state=EXECUTION_FAILED_BEFORE_EXEC,
            exec_exit_code=None,
        )
        evidence = json.loads(
            (candidate / "diagnostics" / "control-envelope.json").read_text(encoding="utf-8")
        )
        manifest = json.loads((candidate / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert evidence["host_attestation"]["exec_exit_code"] is None
        assert evidence["host_attestation"]["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert manifest.get("exit_code") is None
        validate_evidence_manifest_sync(candidate)

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from bridle.features.project_map.store import ProjectPlanStore


def _payload(root: Path, *, classification: str, exit_code: int) -> dict:
    artifact = root / ".bridle" / "artifacts" / f"{classification}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"classification": classification}), encoding="utf-8")
    return {
        "run_id": "run-evidence",
        "node_id": "node-evidence",
        "candidate_id": "candidate-evidence",
        "submission_id": "submission-2",
        "contract_version": "contract-v3",
        "test_code_hash": "test-code-hash",
        "candidate_code_hash": "candidate-code-hash",
        "required_command_ids": ["TEST-UNIT", "TEST-INTEGRATION"],
        "map_seq": 23,
        "boundary_fingerprint": "boundary-v1",
        "image_version": "image@sha256:abc",
        "exit_code": exit_code,
        "duration_ms": 41,
        "classification": classification,
        "changed_paths": ["src/a.py", "tests/test_a.py"],
        "artifact_ref": artifact.relative_to(root).as_posix(),
        "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "source_code": "must not be stored",
        "diff": "must not be stored",
        "stdout": "token=must-not-be-stored",
    }


def test_authoritative_evidence_chain_detects_first_break(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="project-evidence")
    store.ensure_schema()

    red = store.append_evidence(
        node_id="node-evidence",
        event="red_verified",
        payload=_payload(test_workspace, classification="EXPECTED_RED", exit_code=1),
    )
    submitted = store.append_evidence(
        node_id="node-evidence",
        event="submission_frozen",
        payload=_payload(test_workspace, classification="SUBMITTED", exit_code=0),
    )
    published = store.append_evidence(
        node_id="node-evidence",
        event="published",
        payload=_payload(test_workspace, classification="PASSED", exit_code=0),
    )

    assert [red["evidence_seq"], submitted["evidence_seq"], published["evidence_seq"]] == [
        1,
        2,
        3,
    ]
    assert red["previous_hash"] is None
    assert submitted["previous_hash"] == red["evidence_hash"]
    assert published["previous_hash"] == submitted["evidence_hash"]
    for record in store.list_evidence("node-evidence"):
        assert "source_code" not in record["payload"]
        assert "diff" not in record["payload"]
        assert "stdout" not in record["payload"]

    restarted = ProjectPlanStore(test_workspace, project_id="project-evidence")
    valid = restarted.validate_evidence_chain("node-evidence")
    assert valid == {
        "valid": True,
        "node_id": "node-evidence",
        "event_count": 3,
        "latest_evidence_hash": published["evidence_hash"],
        "first_break": None,
        "publication": {
            "submission_id": "submission-2",
            "candidate_code_hash": "candidate-code-hash",
            "changed_paths": ["src/a.py", "tests/test_a.py"],
            "artifact_ref": ".bridle/artifacts/PASSED.json",
        },
    }

    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        row = connection.execute(
            "SELECT payload FROM verification_evidence "
            "WHERE node_id = ? AND evidence_seq = 2",
            ("node-evidence",),
        ).fetchone()
        payload = json.loads(row[0])
        payload["candidate_code_hash"] = "tampered"
        connection.execute(
            "UPDATE verification_evidence SET payload = ? "
            "WHERE node_id = ? AND evidence_seq = 2",
            (json.dumps(payload), "node-evidence"),
        )

    broken = restarted.validate_evidence_chain("node-evidence")
    assert broken["valid"] is False
    assert broken["first_break"] == {
        "evidence_seq": 2,
        "error_code": "evidence_hash_mismatch",
        "artifact_ref": ".bridle/artifacts/SUBMITTED.json",
    }

    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM verification_evidence "
                "WHERE node_id = ? AND evidence_seq = 1",
                ("node-evidence",),
            ).fetchone()[0]
        )
        payload["required_command_ids"] = []
        connection.execute(
            "UPDATE verification_evidence SET payload = ? "
            "WHERE node_id = ? AND evidence_seq = 1",
            (json.dumps(payload), "node-evidence"),
        )
    missing_required = restarted.validate_evidence_chain("node-evidence")
    assert missing_required["first_break"]["error_code"] == "required_command_missing"

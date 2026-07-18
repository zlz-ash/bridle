"""Project-local SQLite storage for the Bridle plan and code map."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bridle.api.errors import ConflictError, NotFoundError, ValidationError
from bridle.features.project_map.boundary_service import BoundaryService
from bridle.features.project_map.indexer.blind_spot_detector import BlindSpotDetector
from bridle.features.project_map.indexer.scip_indexer import ScipIndexer
from bridle.features.project_map.indexer.treesitter_indexer import TreeSitterIndexer
from bridle.features.project_map.map_query_service import SUPPORTED_RISKS, MapQueryService
from bridle.features.project_map.modify_loop_service import TDD_GATE_ERROR, ModifyLoopService
from bridle.features.project_map.patch_schemas import PlanPatchSchema
from bridle.features.project_map.runtime_feedback import RuntimeFeedbackService
from bridle.features.project_map.semantic_synthesis_service import SemanticSynthesisService
from bridle.features.workspace.overview_service import WorkspaceOverviewService
from bridle.logging.facade import LoggingFacade, get_logging_facade

SCHEMA_VERSION = "8"
MAX_PAGE_LIMIT = 200
MAX_SUBGRAPH_DEPTH = 5
NODE_STATUSES = {
    "pending",
    "ready",
    "running",
    "completed",
    "failed",
    "blocked",
    "proposed",
    "ratified",
    "mapping",
    "executing",
    "verifying",
    "drifted",
}
MAP_STATUSES = {
    "not_scanned",
    "scanning_structure",
    "structure_ready",
    "semantic_scanning",
    "needs_arbitration",
    "ready",
    "failed",
    "stale",
}
READY_STATUS = "ready"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS map_applied_messages (
    message_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plan_nodes (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    node_order INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    node_type TEXT NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plan_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, kind)
);
CREATE TABLE IF NOT EXISTS code_entities (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS code_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, kind)
);
CREATE TABLE IF NOT EXISTS change_events (
    change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS semantic_annotations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL,
    confidence REAL NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS map_objections (
    id TEXT PRIMARY KEY,
    objection_type TEXT NOT NULL,
    related_node_ids TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '{}',
    suggested_resolution TEXT NOT NULL DEFAULT '{}',
    annotation_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS arbitration_results (
    id TEXT PRIMARY KEY,
    objection_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS execution_refreshes (
    id TEXT PRIMARY KEY,
    execution_node_id TEXT NOT NULL,
    changed_paths TEXT NOT NULL DEFAULT '[]',
    refreshed_paths TEXT NOT NULL DEFAULT '[]',
    execution_summary TEXT NOT NULL,
    test_summary TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_plan_nodes_parent_order
    ON plan_nodes(parent_id, node_order, id);
CREATE INDEX IF NOT EXISTS ix_plan_nodes_status
    ON plan_nodes(status, archived);
CREATE INDEX IF NOT EXISTS ix_plan_edges_target_kind
    ON plan_edges(target_id, kind);
CREATE INDEX IF NOT EXISTS ix_code_entities_parent_path
    ON code_entities(parent_id, path);
CREATE INDEX IF NOT EXISTS ix_change_events_seq
    ON change_events(change_seq);
CREATE INDEX IF NOT EXISTS ix_map_objections_status
    ON map_objections(status, created_at);
CREATE TABLE IF NOT EXISTS code_symbols (
    moniker       TEXT PRIMARY KEY,
    def_entity_id TEXT,
    kind          TEXT,
    display_name  TEXT
);
CREATE TABLE IF NOT EXISTS code_occurrences (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    moniker   TEXT NOT NULL,
    role      TEXT NOT NULL,
    range     TEXT
);
CREATE INDEX IF NOT EXISTS ix_occ_file ON code_occurrences(file_path);
CREATE INDEX IF NOT EXISTS ix_occ_moniker ON code_occurrences(moniker);
CREATE TABLE IF NOT EXISTS code_cochange (
    path_a     TEXT NOT NULL,
    path_b     TEXT NOT NULL,
    co_count   INTEGER NOT NULL,
    sup_a      INTEGER NOT NULL,
    sup_b      INTEGER NOT NULL,
    weight     REAL NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (path_a, path_b)
);
CREATE TABLE IF NOT EXISTS module_metrics (
    module_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    change_seq  INTEGER,
    computed_at TEXT,
    PRIMARY KEY (module_id, metric)
);
CREATE TABLE IF NOT EXISTS map_blind_spots (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    file_path   TEXT,
    range       TEXT,
    detail      TEXT,
    source      TEXT,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_blind_spots_status ON map_blind_spots(status, file_path);
CREATE TABLE IF NOT EXISTS module_interfaces (
    id          TEXT PRIMARY KEY,
    from_module TEXT NOT NULL,
    to_module   TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    signature   TEXT,
    mock        TEXT,
    confidence  REAL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS child_spawn_facts (
    message_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE,
    target_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS child_result_receipts (
    message_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    result_status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS semantic_map_runs (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS semantic_evidence_bundles (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS module_candidates (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    module_id        TEXT NOT NULL,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL,
    confidence       REAL NOT NULL,
    evidence_id      TEXT NOT NULL,
    metrics          TEXT NOT NULL DEFAULT '{}',
    file_fingerprint TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_module_candidates_status
    ON module_candidates(status, module_id);
CREATE TABLE IF NOT EXISTS module_candidate_files (
    candidate_id TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    role         TEXT NOT NULL,
    file_hash    TEXT NOT NULL,
    evidence     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (candidate_id, file_path)
);
CREATE INDEX IF NOT EXISTS ix_module_candidate_files_path
    ON module_candidate_files(file_path);
CREATE TABLE IF NOT EXISTS module_edges (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    source_candidate_id TEXT NOT NULL,
    target_candidate_id TEXT NOT NULL,
    source_module       TEXT NOT NULL,
    target_module       TEXT NOT NULL,
    kind                TEXT NOT NULL,
    weight              REAL NOT NULL,
    evidence            TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS module_interface_candidates (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    from_module       TEXT NOT NULL,
    to_module         TEXT NOT NULL,
    from_candidate_id TEXT NOT NULL,
    to_candidate_id   TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    signature         TEXT NOT NULL DEFAULT '{}',
    evidence          TEXT NOT NULL DEFAULT '{}',
    mock_file_path    TEXT NOT NULL DEFAULT '',
    mock_hash         TEXT NOT NULL DEFAULT '',
    confidence        REAL NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_module_interface_candidates_status
    ON module_interface_candidates(status, from_module, to_module);
CREATE TABLE IF NOT EXISTS interface_mock_artifacts (
    id                     TEXT PRIMARY KEY,
    interface_candidate_id TEXT NOT NULL,
    file_path              TEXT NOT NULL,
    file_hash              TEXT NOT NULL,
    status                 TEXT NOT NULL,
    payload                TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS modification_workflows (
    node_id               TEXT PRIMARY KEY,
    state                 TEXT NOT NULL,
    revision              INTEGER NOT NULL,
    test_contract_version TEXT,
    updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS modification_events (
    sequence   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    node_id    TEXT NOT NULL,
    event      TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_modification_events_node_sequence
    ON modification_events(node_id, sequence);
CREATE TABLE IF NOT EXISTS test_contracts (
    node_id             TEXT NOT NULL,
    contract_version    TEXT NOT NULL,
    snapshot            TEXT NOT NULL,
    state               TEXT NOT NULL CHECK(state IN ('frozen', 'invalidated')),
    invalidation_reason TEXT,
    frozen_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    invalidated_at      TEXT,
    PRIMARY KEY(node_id, contract_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_test_contracts_active_node
    ON test_contracts(node_id) WHERE state = 'frozen';
CREATE TABLE IF NOT EXISTS verification_runs (
    sequence         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL UNIQUE,
    node_id          TEXT NOT NULL,
    phase            TEXT NOT NULL CHECK(phase IN ('red', 'final')),
    source_revision  INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    candidate_id     TEXT,
    state            TEXT NOT NULL CHECK(state IN ('queued', 'running', 'completed', 'failed')),
    attempt          INTEGER NOT NULL DEFAULT 0,
    next_retry_at    REAL,
    max_attempts     INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
    terminal_reason  TEXT,
    lease_token      TEXT,
    lease_expires_at REAL,
    outcome          TEXT,
    error_code       TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at     TEXT,
    UNIQUE(node_id, phase, source_revision, contract_version)
);
CREATE INDEX IF NOT EXISTS ix_verification_runs_recovery
    ON verification_runs(state, lease_expires_at, sequence);
CREATE INDEX IF NOT EXISTS ix_verification_runs_node_sequence
    ON verification_runs(node_id, sequence);
CREATE TABLE IF NOT EXISTS verification_evidence (
    node_id       TEXT NOT NULL,
    evidence_seq  INTEGER NOT NULL,
    event         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    previous_hash TEXT,
    evidence_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(node_id, evidence_seq)
);
CREATE TABLE IF NOT EXISTS stage_events (
    sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id       TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    candidate_id   TEXT,
    submission_id  TEXT,
    run_id         TEXT,
    wait_id        TEXT,
    message_id     TEXT,
    stage          TEXT NOT NULL,
    status         TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    started_at     TEXT,
    ended_at       TEXT,
    duration_ms    INTEGER,
    error_code     TEXT,
    retry_reason   TEXT,
    artifact_ref   TEXT,
    cleanup_status TEXT,
    detail         TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_stage_events_trace_sequence
    ON stage_events(trace_id, sequence);
CREATE TABLE IF NOT EXISTS node_executions (
    execution_id  TEXT PRIMARY KEY,
    wait_id       TEXT NOT NULL UNIQUE,
    node_id       TEXT NOT NULL,
    owner_address TEXT NOT NULL,
    state         TEXT NOT NULL CHECK(state IN ('waiting', 'ended')),
    phase         TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    outcome       TEXT CHECK(outcome IN ('completed', 'failed', 'blocked', 'cancelled')),
    result_ref    TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_node_executions_active_node
    ON node_executions(node_id) WHERE state = 'waiting';
CREATE TABLE IF NOT EXISTS wait_signals (
    wait_id      TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE,
    node_id      TEXT NOT NULL,
    state        TEXT NOT NULL CHECK(state IN ('waiting', 'ended')),
    phase        TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    outcome      TEXT CHECK(outcome IN ('completed', 'failed', 'blocked', 'cancelled')),
    result_ref   TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at     TEXT,
    FOREIGN KEY(execution_id) REFERENCES node_executions(execution_id)
);
CREATE TABLE IF NOT EXISTS completion_outbox (
    wait_id       TEXT PRIMARY KEY,
    execution_id  TEXT NOT NULL,
    owner_address TEXT NOT NULL,
    message_id    TEXT NOT NULL UNIQUE,
    payload        TEXT NOT NULL,
    state          TEXT NOT NULL CHECK(state IN ('pending', 'sent')),
    attempt        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at        TEXT,
    FOREIGN KEY(wait_id) REFERENCES wait_signals(wait_id),
    FOREIGN KEY(execution_id) REFERENCES node_executions(execution_id)
);
CREATE INDEX IF NOT EXISTS ix_completion_outbox_state_created
    ON completion_outbox(state, created_at, wait_id);
"""


class ProjectPlanStore:
    """Own one `.bridle/plan.db`; constructor input selects project and later methods return map data."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        project_id: str,
        facade: LoggingFacade | None = None,
    ) -> None:
        """Bind a project root and ID; output is a store with no I/O until a method is called."""
        self.project_root = Path(project_root).resolve()
        self.project_id = project_id
        self.database_path = self.project_root / ".bridle" / "plan.db"
        self._facade = facade or get_logging_facade()
        self._indexer = TreeSitterIndexer(facade=self._facade)
        self._scip = ScipIndexer(facade=self._facade)
        self._blind_spots = BlindSpotDetector()
        self._map_query = MapQueryService(self.project_root)
        self._boundary = BoundaryService(self.project_root)
        self._synthesis = SemanticSynthesisService(self.project_root)
        self._runtime_feedback = RuntimeFeedbackService()
        self._active_semantic_run_id: str | None = None
        self._semantic_continuing_from_failure = False
        self._semantic_resuming_interrupted_run = False

    @classmethod
    def open_existing(
        cls,
        project_root: str | Path,
        *,
        facade: LoggingFacade | None = None,
    ) -> ProjectPlanStore:
        """Open an existing map; project path input returns a store using its persisted project ID."""
        root = Path(project_root).resolve()
        database_path = root / ".bridle" / "plan.db"
        if not database_path.is_file():
            raise NotFoundError(resource="project_map", message="plan.db not found")
        connection = sqlite3.connect(database_path)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'project_id'"
            ).fetchone()
        finally:
            connection.close()
        if row is None or not str(row[0]):
            raise ValidationError(resource="project_map", message="plan.db project_id is missing")
        store = cls(root, project_id=str(row[0]), facade=facade)
        store.ensure_schema()
        return store

    def ensure_schema(self) -> None:
        """Create or migrate only the project-local SQLite schema."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_existing_metadata()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._initialize_metadata(connection)
            self._migrate_schema(connection)

    def append_evidence(
        self,
        *,
        node_id: str,
        event: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one authoritative verification event to the node-local hash chain."""
        started = time.perf_counter()
        safe_payload = self._sanitize_evidence_payload(node_id=node_id, payload=payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT evidence_seq, evidence_hash FROM verification_evidence "
                "WHERE node_id = ? ORDER BY evidence_seq DESC LIMIT 1",
                (node_id,),
            ).fetchone()
            evidence_seq = int(previous["evidence_seq"]) + 1 if previous else 1
            previous_hash = str(previous["evidence_hash"]) if previous else None
            evidence_hash = self._evidence_hash(
                node_id=node_id,
                evidence_seq=evidence_seq,
                event=event,
                payload=safe_payload,
                previous_hash=previous_hash,
            )
            connection.execute(
                "INSERT INTO verification_evidence("
                "node_id, evidence_seq, event, payload, previous_hash, evidence_hash"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    node_id,
                    evidence_seq,
                    event,
                    self._canonical_json(safe_payload),
                    previous_hash,
                    evidence_hash,
                ),
            )
        record = {
            "node_id": node_id,
            "evidence_seq": evidence_seq,
            "event": event,
            "payload": safe_payload,
            "previous_hash": previous_hash,
            "evidence_hash": evidence_hash,
        }
        self._log(
            "verification_evidence_append",
            "completed",
            node_id=node_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={"evidence_seq": evidence_seq, "event": event},
        )
        return record

    def list_evidence(self, node_id: str) -> list[dict[str, Any]]:
        """Return one node's evidence chain in append order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT node_id, evidence_seq, event, payload, previous_hash, "
                "evidence_hash, created_at FROM verification_evidence "
                "WHERE node_id = ? ORDER BY evidence_seq",
                (node_id,),
            ).fetchall()
        return [
            {
                "node_id": str(row["node_id"]),
                "evidence_seq": int(row["evidence_seq"]),
                "event": str(row["event"]),
                "payload": json.loads(str(row["payload"])),
                "previous_hash": row["previous_hash"],
                "evidence_hash": str(row["evidence_hash"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def validate_evidence_chain(self, node_id: str) -> dict[str, Any]:
        """Validate one node's evidence and return the first broken contract boundary."""
        started = time.perf_counter()
        records = self.list_evidence(node_id)
        previous_hash: str | None = None
        first_break: dict[str, Any] | None = None
        publication: dict[str, Any] | None = None
        for record in records:
            payload = record["payload"]
            artifact_ref = str(payload.get("artifact_ref") or "")
            if not payload.get("required_command_ids"):
                first_break = self._evidence_break(
                    record, error_code="required_command_missing"
                )
                break
            artifact_error = self._validate_evidence_artifact(payload)
            if artifact_error is not None:
                first_break = self._evidence_break(record, error_code=artifact_error)
                break
            if record["previous_hash"] != previous_hash:
                first_break = self._evidence_break(
                    record, error_code="evidence_previous_hash_mismatch"
                )
                break
            expected_hash = self._evidence_hash(
                node_id=node_id,
                evidence_seq=record["evidence_seq"],
                event=record["event"],
                payload=payload,
                previous_hash=previous_hash,
            )
            if record["evidence_hash"] != expected_hash:
                first_break = self._evidence_break(
                    record, error_code="evidence_hash_mismatch"
                )
                break
            previous_hash = record["evidence_hash"]
            if record["event"] == "published":
                publication = {
                    "submission_id": payload.get("submission_id"),
                    "candidate_code_hash": payload.get("candidate_code_hash"),
                    "changed_paths": payload.get("changed_paths", []),
                    "artifact_ref": artifact_ref,
                }
        valid = first_break is None
        result = {
            "valid": valid,
            "node_id": node_id,
            "event_count": len(records),
            "latest_evidence_hash": records[-1]["evidence_hash"] if records else None,
            "first_break": first_break,
            "publication": publication,
        }
        self._log(
            "verification_evidence_validate",
            "completed" if valid else "failed",
            node_id=node_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "event_count": len(records),
                "error_code": first_break["error_code"] if first_break else None,
            },
        )
        return result

    def record_stage_event(
        self,
        *,
        trace_id: str,
        node_id: str,
        candidate_id: str | None,
        submission_id: str | None,
        run_id: str | None,
        wait_id: str | None,
        message_id: str | None,
        stage: str,
        status: str,
        attempt: int,
        started_at: str | None,
        ended_at: str | None,
        duration_ms: int | None,
        error_code: str | None,
        retry_reason: str | None,
        artifact_ref: str | None,
        cleanup_status: str | None,
        detail: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Persist one sanitized workflow stage event in the project fact source."""
        started = time.perf_counter()
        safe_detail = self._sanitize_stage_detail(detail or {})
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO stage_events("
                "trace_id, node_id, candidate_id, submission_id, run_id, wait_id, "
                "message_id, stage, status, attempt, started_at, ended_at, duration_ms, "
                "error_code, retry_reason, artifact_ref, cleanup_status, detail"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    node_id,
                    candidate_id,
                    submission_id,
                    run_id,
                    wait_id,
                    message_id,
                    stage,
                    status,
                    attempt,
                    started_at,
                    ended_at,
                    duration_ms,
                    error_code,
                    retry_reason,
                    artifact_ref,
                    cleanup_status,
                    self._canonical_json(safe_detail),
                ),
            )
            sequence = int(cursor.lastrowid)
        record = {
            "sequence": sequence,
            "trace_id": trace_id,
            "node_id": node_id,
            "candidate_id": candidate_id,
            "submission_id": submission_id,
            "run_id": run_id,
            "wait_id": wait_id,
            "message_id": message_id,
            "stage": stage,
            "status": status,
            "attempt": attempt,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "retry_reason": retry_reason,
            "artifact_ref": artifact_ref,
            "cleanup_status": cleanup_status,
            "detail": safe_detail,
        }
        self._log(
            "workflow_stage_event_record",
            "completed",
            node_id=node_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "trace_id": trace_id,
                "sequence": sequence,
                "stage": stage,
                "stage_status": status,
                "attempt": attempt,
                "error_code": error_code,
            },
        )
        return record

    def list_stage_events(self, trace_id: str) -> list[dict[str, Any]]:
        """Return a durable workflow timeline ordered by its global append sequence."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stage_events WHERE trace_id = ? ORDER BY sequence",
                (trace_id,),
            ).fetchall()
        events = [
            {
                "sequence": int(row["sequence"]),
                "trace_id": str(row["trace_id"]),
                "node_id": str(row["node_id"]),
                "candidate_id": row["candidate_id"],
                "submission_id": row["submission_id"],
                "run_id": row["run_id"],
                "wait_id": row["wait_id"],
                "message_id": row["message_id"],
                "stage": str(row["stage"]),
                "status": str(row["status"]),
                "attempt": int(row["attempt"]),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration_ms": row["duration_ms"],
                "error_code": row["error_code"],
                "retry_reason": row["retry_reason"],
                "artifact_ref": row["artifact_ref"],
                "cleanup_status": row["cleanup_status"],
                "detail": json.loads(str(row["detail"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
        self._log(
            "workflow_stage_timeline_read",
            "completed",
            node_id=events[0]["node_id"] if events else None,
            detail={"trace_id": trace_id, "event_count": len(events)},
        )
        return events

    def create_node_execution(
        self,
        *,
        node_id: str,
        owner_address: str,
    ) -> dict[str, Any]:
        """Create or reuse the one active execution and durable wait for a node."""
        started = time.perf_counter()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                self._execution_select_sql()
                + " WHERE e.node_id = ? AND e.state = 'waiting'",
                (node_id,),
            ).fetchone()
            if existing is not None:
                record = self._node_execution_from_row(existing)
            else:
                execution_id = f"execution-{uuid.uuid4().hex}"
                wait_id = f"wait-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO node_executions("
                    "execution_id, wait_id, node_id, owner_address, state, phase, revision"
                    ") VALUES (?, ?, ?, ?, 'waiting', 'queued', 1)",
                    (execution_id, wait_id, node_id, owner_address),
                )
                connection.execute(
                    "INSERT INTO wait_signals("
                    "wait_id, execution_id, node_id, state, phase, revision"
                    ") VALUES (?, ?, ?, 'waiting', 'queued', 1)",
                    (wait_id, execution_id, node_id),
                )
                row = connection.execute(
                    self._execution_select_sql() + " WHERE w.wait_id = ?",
                    (wait_id,),
                ).fetchone()
                assert row is not None
                record = self._node_execution_from_row(row)
        self._log(
            "node_execution_create",
            "completed",
            node_id=node_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "execution_id": record["execution_id"],
                "wait_id": record["wait_id"],
                "state": record["state"],
                "reused": existing is not None,
            },
        )
        return record

    def complete_execution(
        self,
        *,
        wait_id: str,
        outcome: str,
        result_ref: str | None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        """Atomically end execution and wait state and create one completion outbox row."""
        if outcome not in {"completed", "failed", "blocked", "cancelled"}:
            raise ValidationError(
                resource="node_execution",
                message="Invalid terminal execution outcome",
            )
        started = time.perf_counter()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._execution_select_sql() + " WHERE w.wait_id = ?",
                (wait_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(resource="wait_signal", message="wait signal not found")
            current = self._node_execution_from_row(row)
            if current["state"] == "ended":
                if current["outcome"] != outcome or current["result_ref"] != result_ref:
                    raise ConflictError(
                        resource="node_execution",
                        message="execution already ended with another outcome",
                        error_code="execution_terminal_conflict",
                    )
                record = current
            else:
                terminal_phase = phase or current["phase"]
                connection.execute(
                    "UPDATE node_executions SET state = 'ended', phase = ?, outcome = ?, "
                    "result_ref = ?, updated_at = CURRENT_TIMESTAMP, ended_at = CURRENT_TIMESTAMP "
                    "WHERE execution_id = ? AND state = 'waiting'",
                    (terminal_phase, outcome, result_ref, current["execution_id"]),
                )
                connection.execute(
                    "UPDATE wait_signals SET state = 'ended', phase = ?, outcome = ?, "
                    "result_ref = ?, updated_at = CURRENT_TIMESTAMP, ended_at = CURRENT_TIMESTAMP "
                    "WHERE wait_id = ? AND state = 'waiting'",
                    (terminal_phase, outcome, result_ref, wait_id),
                )
                payload = {
                    "wait_id": wait_id,
                    "execution_id": current["execution_id"],
                    "node_id": current["node_id"],
                    "state": "ended",
                    "outcome": outcome,
                    "result_ref": result_ref,
                    "revision": current["revision"],
                }
                connection.execute(
                    "INSERT OR IGNORE INTO completion_outbox("
                    "wait_id, execution_id, owner_address, message_id, payload, state"
                    ") VALUES (?, ?, ?, ?, ?, 'pending')",
                    (
                        wait_id,
                        current["execution_id"],
                        current["owner_address"],
                        f"node-workflow-result:{wait_id}",
                        self._canonical_json(payload),
                    ),
                )
                ended_row = connection.execute(
                    self._execution_select_sql() + " WHERE w.wait_id = ?",
                    (wait_id,),
                ).fetchone()
                assert ended_row is not None
                record = self._node_execution_from_row(ended_row)
        self._log(
            "node_execution_complete",
            "completed",
            node_id=record["node_id"],
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "execution_id": record["execution_id"],
                "wait_id": wait_id,
                "outcome": record["outcome"],
                "result_ref": record["result_ref"],
            },
        )
        return record

    def read_execution(self, wait_id: str) -> dict[str, Any] | None:
        """Read a wait without dispatching, retrying, or changing execution state."""
        with self._connect() as connection:
            row = connection.execute(
                self._execution_select_sql() + " WHERE w.wait_id = ?",
                (wait_id,),
            ).fetchone()
        return self._node_execution_from_row(row) if row is not None else None

    def list_active_node_executions(self) -> list[dict[str, Any]]:
        """List durable waiting executions for process-restart recovery."""
        with self._connect() as connection:
            rows = connection.execute(
                self._execution_select_sql()
                + " WHERE e.state = 'waiting' ORDER BY e.created_at, e.execution_id"
            ).fetchall()
        return [self._node_execution_from_row(row) for row in rows]

    def list_pending_completion_outbox(self) -> list[dict[str, Any]]:
        """List terminal notifications that have not reached the persistent mailbox."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT wait_id, execution_id, owner_address, message_id, payload, attempt "
                "FROM completion_outbox WHERE state = 'pending' "
                "ORDER BY created_at, wait_id"
            ).fetchall()
        return [
            {
                "wait_id": str(row["wait_id"]),
                "execution_id": str(row["execution_id"]),
                "owner_address": str(row["owner_address"]),
                "message_id": str(row["message_id"]),
                "payload": json.loads(str(row["payload"])),
                "attempt": int(row["attempt"]),
            }
            for row in rows
        ]

    def mark_completion_outbox_sent(self, wait_id: str) -> None:
        """Mark a completion delivered only after mailbox insertion or idempotent existence."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE completion_outbox SET state = 'sent', attempt = attempt + 1, "
                "updated_at = CURRENT_TIMESTAMP, sent_at = CURRENT_TIMESTAMP "
                "WHERE wait_id = ? AND state = 'pending'",
                (wait_id,),
            )
        self._log(
            "completion_outbox_forward",
            "completed",
            detail={"wait_id": wait_id},
        )

    def mark_completion_outbox_attempt(self, wait_id: str, *, error_code: str) -> None:
        """Record a failed forward while leaving the durable outbox pending."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE completion_outbox SET attempt = attempt + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE wait_id = ? AND state = 'pending'",
                (wait_id,),
            )
        self._log(
            "completion_outbox_forward",
            "retry",
            detail={"wait_id": wait_id, "error_code": error_code},
        )

    @staticmethod
    def _execution_select_sql() -> str:
        return (
            "SELECT w.wait_id, e.execution_id, e.node_id, e.owner_address, "
            "w.state, w.phase, w.revision, w.outcome, w.result_ref "
            "FROM wait_signals w JOIN node_executions e "
            "ON e.execution_id = w.execution_id"
        )

    @staticmethod
    def _node_execution_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "wait_id": str(row["wait_id"]),
            "execution_id": str(row["execution_id"]),
            "node_id": str(row["node_id"]),
            "owner_address": str(row["owner_address"]),
            "state": str(row["state"]),
            "phase": str(row["phase"]),
            "revision": int(row["revision"]),
            "outcome": row["outcome"],
            "result_ref": row["result_ref"],
        }

    @staticmethod
    def _sanitize_stage_detail(detail: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "preview",
            "container_id",
            "command_id",
            "classification",
            "evidence_hash",
            "candidate_code_hash",
            "submission_revision",
            "required_command_ids",
            "changed_paths",
            "exit_code",
            "duration_ms",
            "event_count",
            "error_code",
            "cleanup_error",
            "message_type",
        }
        safe_detail = {key: detail[key] for key in allowed if key in detail}
        preview = safe_detail.get("preview")
        if isinstance(preview, str):
            safe_detail["preview"] = preview[:2048]
        return safe_detail

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _evidence_hash(
        cls,
        *,
        node_id: str,
        evidence_seq: int,
        event: str,
        payload: dict[str, Any],
        previous_hash: str | None,
    ) -> str:
        material = cls._canonical_json(
            {
                "node_id": node_id,
                "evidence_seq": evidence_seq,
                "event": event,
                "payload": payload,
                "previous_hash": previous_hash,
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _sanitize_evidence_payload(
        self,
        *,
        node_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "run_id",
            "node_id",
            "candidate_id",
            "submission_id",
            "contract_version",
            "test_code_hash",
            "candidate_code_hash",
            "required_command_ids",
            "map_seq",
            "boundary_fingerprint",
            "image_version",
            "exit_code",
            "duration_ms",
            "classification",
            "changed_paths",
            "artifact_ref",
            "artifact_digest",
        }
        safe_payload = {key: payload[key] for key in allowed if key in payload}
        if safe_payload.get("node_id") != node_id:
            raise ValidationError(
                resource="verification_evidence",
                message="evidence node_id does not match the chain node",
            )
        if not safe_payload.get("required_command_ids"):
            raise ValidationError(
                resource="verification_evidence",
                message="required_command_ids must not be empty",
            )
        artifact_error = self._validate_evidence_artifact(safe_payload)
        if artifact_error is not None:
            raise ValidationError(resource="verification_evidence", message=artifact_error)
        return safe_payload

    def _validate_evidence_artifact(self, payload: dict[str, Any]) -> str | None:
        artifact_ref = str(payload.get("artifact_ref") or "")
        artifact_digest = str(payload.get("artifact_digest") or "")
        if not artifact_ref or not artifact_digest:
            return "artifact_reference_missing"
        artifact_path = (self.project_root / artifact_ref).resolve()
        try:
            artifact_path.relative_to(self.project_root)
        except ValueError:
            return "artifact_reference_invalid"
        if not artifact_path.is_file():
            return "artifact_missing"
        actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if actual_digest != artifact_digest:
            return "artifact_digest_mismatch"
        return None

    @staticmethod
    def _evidence_break(record: dict[str, Any], *, error_code: str) -> dict[str, Any]:
        return {
            "evidence_seq": record["evidence_seq"],
            "error_code": error_code,
            "artifact_ref": record["payload"].get("artifact_ref"),
        }

    def initialize(self, *, scan_if_created: bool = True) -> dict[str, Any]:
        """Open/create the DB and first-scan a workspace; output reports creation and scan state."""
        started = time.perf_counter()
        created = not self.database_path.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._log("project_map_initialize", "started", detail={"created": created})

        try:
            self.ensure_schema()
            if created and scan_if_created:
                self.rescan()
            self._recover_interrupted_semantic_scan()
            self._maybe_run_semantic_scan()
            status = self._metadata("scan_status") or "not_scanned"
            if status == "completed":
                self.mark_map_status(READY_STATUS, reason="migrated_completed_status")
                status = READY_STATUS
            entity_count = self._count("code_entities")
        except Exception as exc:
            self._log(
                "project_map_initialize",
                "failed",
                detail={"error_code": type(exc).__name__},
                duration_ms=self._elapsed_ms(started),
            )
            raise

        result = {
            "created": created,
            "scan_status": status,
            "entity_count": entity_count,
            "database_path": str(self.database_path),
            **self.readiness(status),
        }
        self._log(
            "project_map_initialize",
            "completed",
            detail={"created": created, "scan_status": status, "entity_count": entity_count},
            duration_ms=self._elapsed_ms(started),
        )
        return result

    def rescan(self) -> dict[str, Any]:
        """Run the existing workspace scanner; output replaces code-map rows or records failed state."""
        started = time.perf_counter()
        self._log("project_map_scan", "started")
        try:
            entities = WorkspaceOverviewService.scan_entities(self.project_root)
            test_dirs = self._declared_test_dirs()
            file_entities = [entity for entity in entities if entity["kind"] == "file"]
            index = self._indexer.run(
                self.project_root,
                file_entities=file_entities,
                parse_paths=None,
                test_dirs=test_dirs,
            )
            entities = self._apply_test_classification(entities, index.test_paths)
            entities = entities + index.symbol_entities
            file_paths = [
                entity["path"]
                for entity in entities
                if entity["kind"] == "file"
            ]
            nontest = {
                entity["path"]
                for entity in entities
                if entity["kind"] in ("file",)
            }
            scip = self._scip.index_paths(
                self.project_root,
                file_paths,
                file_entities=[e for e in entities if e["kind"] == "file"],
                nontest_files=nontest,
            )
            blind_rows = self._collect_blind_spots(file_paths, nontest)
            with self._connect() as connection:
                self._set_map_status(
                    connection,
                    "scanning_structure",
                    reason="deterministic_scan_started",
                )
                self._replace_code_map(connection, entities, index.relations + scip.relations)
                self._replace_scip_data(connection, scip)
                self._replace_static_blind_spots(connection, blind_rows)
                self._boundary.refresh_cochange(connection)
                self._boundary.compute_metrics(connection, change_seq=self._latest_change_seq(connection))
                self._refresh_semantic_map_candidates(connection, reason="structure_scan")
                self._set_map_status(
                    connection,
                    "structure_ready",
                    reason="deterministic_scan_completed",
                )
                self._set_metadata(connection, "semantic_scan_status", "pending")
                self._set_metadata(connection, "semantic_scan_run_id", "")
                self._set_metadata(connection, "semantic_scan_interrupted", "0")
                self._set_metadata(connection, "semantic_scan_processed", "0")
                self._set_metadata(connection, "semantic_scan_routed", "0")
                self._set_metadata(connection, "semantic_scan_deferred", "0")
                self._set_metadata(connection, "semantic_scan_remaining", "0")
            result = {
                "scan_status": "structure_ready",
                "entity_count": len(entities),
                **self.readiness("structure_ready"),
            }
            self._log(
                "project_map_scan",
                "completed",
                detail=result,
                duration_ms=self._elapsed_ms(started),
            )
            return result
        except Exception as exc:
            with self._connect() as connection:
                self._set_map_status(connection, "failed", reason=type(exc).__name__)
            self._log(
                "project_map_scan",
                "failed",
                detail={"error_code": type(exc).__name__},
                duration_ms=self._elapsed_ms(started),
            )
            return {"scan_status": "failed", "entity_count": self._count("code_entities"), **self.readiness("failed")}

    def refresh_code_paths(self, rel_paths: list[str]) -> dict[str, Any]:
        """Refresh changed or deleted paths; rebuilds cross-file edges without full rescan."""
        started = time.perf_counter()
        normalized = sorted({self._normalize_relative_path(path) for path in rel_paths})
        self._log("project_map_incremental_refresh", "started", detail={"path_count": len(normalized)})
        with self._connect() as connection:
            self._refresh_code_paths_in_connection(connection, normalized)
        result = {"refreshed_paths": normalized}
        self._log(
            "project_map_incremental_refresh",
            "completed",
            detail={"path_count": len(normalized)},
            duration_ms=self._elapsed_ms(started),
        )
        return result

    def apply_code_changed_batch(
        self,
        messages: list[tuple[str, list[str]]],
    ) -> dict[str, list[str]]:
        """Atomically refresh paths and persist one idempotency receipt per new message."""
        message_ids = [message_id for message_id, _paths in messages]
        if any(not message_id for message_id in message_ids) or len(set(message_ids)) != len(message_ids):
            raise ValidationError(resource="project_map_message", message="Invalid message batch")
        with self._connect() as connection:
            existing: set[str] = set()
            if message_ids:
                placeholders = ",".join("?" for _item in message_ids)
                existing = {
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT message_id FROM map_applied_messages WHERE message_id IN ({placeholders})",
                        message_ids,
                    ).fetchall()
                }
            fresh = [(message_id, paths) for message_id, paths in messages if message_id not in existing]
            normalized = sorted(
                {
                    self._normalize_relative_path(path)
                    for _message_id, paths in fresh
                    for path in paths
                }
            )
            if normalized:
                self._refresh_code_paths_in_connection(connection, normalized)
            connection.executemany(
                "INSERT INTO map_applied_messages(message_id) VALUES (?)",
                [(message_id,) for message_id, _paths in fresh],
            )
        applied = [message_id for message_id, _paths in fresh]
        duplicate = [message_id for message_id in message_ids if message_id in existing]
        self._log(
            "project_map_message_batch",
            "completed",
            detail={
                "message_count": len(messages),
                "applied_count": len(applied),
                "duplicate_count": len(duplicate),
                "path_count": len(normalized),
            },
        )
        return {
            "applied_message_ids": applied,
            "duplicate_message_ids": duplicate,
            "refreshed_paths": normalized,
        }

    def _refresh_code_paths_in_connection(
        self,
        connection: sqlite3.Connection,
        normalized: list[str],
    ) -> dict[str, list[str]]:
        normalized_set = set(normalized)
        parse_paths = [
            rel_path
            for rel_path in normalized
            if self.project_root.joinpath(*rel_path.split("/")).is_file()
        ]
        test_dirs = self._declared_test_dirs(connection)
        all_stale_ids = self._entity_ids_for_paths(connection, normalized)
        rebuild_paths = sorted(
            self._collect_relation_rebuild_paths(connection, all_stale_ids, normalized_set)
        )
        annotation_snapshots = self._collect_annotation_snapshots(connection, normalized)
        for rel_path in normalized:
            self._purge_path_artifacts(connection, rel_path)
        connection.execute(
            "DELETE FROM code_symbols WHERE def_entity_id NOT IN (SELECT id FROM code_entities)"
        )
        if parse_paths:
            entities = WorkspaceOverviewService.scan_paths(self.project_root, parse_paths)
            file_entities = self._workspace_file_entities(connection, normalized, entities)
            index = self._indexer.run(
                self.project_root,
                file_entities=file_entities,
                parse_paths=parse_paths,
                test_dirs=test_dirs,
            )
            entities = self._apply_test_classification(entities, index.test_paths)
            self._insert_code_entities(connection, entities, index.symbol_entities, index.relations)
        rebuild_only = [path for path in rebuild_paths if path not in normalized_set]
        if rebuild_only:
            self._rebuild_relations_for_paths(connection, rebuild_only, test_dirs)
        if parse_paths:
            file_entities = self._workspace_file_entities(
                connection,
                parse_paths,
                WorkspaceOverviewService.scan_paths(self.project_root, parse_paths),
            )
            nontest_files = {
                entity["path"] for entity in file_entities if entity.get("kind") in ("file",)
            }
            scip = self._scip.index_paths(
                self.project_root,
                parse_paths,
                file_entities=[entity for entity in file_entities if entity.get("kind") == "file"],
                nontest_files=nontest_files,
            )
            self._replace_scip_for_paths(connection, parse_paths, scip)
            for relation in scip.relations:
                self._insert_relation(connection, relation)
            blind_rows = self._collect_blind_spots(parse_paths, nontest_files)
            self._upsert_blind_spots_for_files(connection, parse_paths, blind_rows)
        self._reconcile_annotations_after_refresh(connection, annotation_snapshots)
        self._boundary.compute_metrics(connection, change_seq=self._latest_change_seq(connection))
        self._refresh_semantic_map_candidates(connection, reason="incremental_refresh")
        divergent = ModifyLoopService.list_divergent_nodes(connection)
        if divergent:
            ModifyLoopService.mark_drifted(connection, divergent)
        for rel_path in normalized:
            self._record_change(
                connection,
                "code_entity",
                WorkspaceOverviewService._entity_id("file", rel_path),
                "refresh",
                {"path": rel_path},
            )
        return {"refreshed_paths": normalized}

    def patch(self, patch: PlanPatchSchema) -> dict[str, Any]:
        """Apply one validated local patch; input is PlanPatchSchema and output lists changed IDs/events."""
        started = time.perf_counter()
        self._log("project_plan_patch", "started", detail={"operation_count": self._patch_size(patch)})
        try:
            with self._connect() as connection:
                self._assert_map_ready(connection)
                self._assert_patch_mutable(connection, patch)
                changed = self._apply_patch(connection, patch)
                self._validate_graph(connection)
            result = {"changed_node_ids": sorted(changed), "change_seq": self._latest_change_seq()}
            self._log(
                "project_plan_patch",
                "completed",
                detail={"changed_count": len(changed), "change_seq": result["change_seq"]},
                duration_ms=self._elapsed_ms(started),
            )
            return result
        except Exception as exc:
            code = getattr(getattr(exc, "api_error", None), "code", type(exc).__name__)
            self._log(
                "project_plan_patch",
                "rejected",
                detail={"error_code": code},
                duration_ms=self._elapsed_ms(started),
            )
            raise

    def set_node_status(self, node_id: str, status: str) -> dict[str, Any]:
        """Set executor-owned status; node/status input returns the updated node and emits one event."""
        if status not in NODE_STATUSES:
            raise ValidationError(
                resource="plan_node",
                message="Unsupported node status",
                details={"status": status},
            )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM plan_nodes WHERE id = ? AND archived = 0", (node_id,),
            ).fetchone()
            if existing is None:
                raise NotFoundError(resource="plan_node", details={"node_id": node_id})
            connection.execute("UPDATE plan_nodes SET status = ? WHERE id = ?", (status, node_id))
            self._record_change(connection, "plan_node", node_id, "status", {"status": status})
        self._log("project_node_status", "completed", node_id=node_id, detail={"status": status})
        return self.get_node(node_id)

    def mark_map_status(self, status: str, *, reason: str) -> dict[str, Any]:
        """Set project-map readiness; status/reason input exits with public readiness fields."""
        if status not in MAP_STATUSES:
            raise ValidationError(
                resource="project_map",
                message="Unsupported project map status",
                details={"status": status},
            )
        with self._connect() as connection:
            self._set_map_status(connection, status, reason=reason)
        self._log("project_map_status", status, detail={"reason": reason})
        return self.readiness(status)

    def mark_map_degraded(self, *, reason: str) -> dict[str, Any]:
        """Persist runtime degradation without creating a business change event."""
        with self._connect() as connection:
            self._set_metadata(connection, "scan_status", "stale")
            self._set_metadata(connection, "readiness_reason", reason)
        self._log("project_map_status", "stale", detail={"reason": reason})
        return self.readiness("stale")

    def mark_semantic_scan_completed(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Complete semantic scan; no input exits ready unless pending objections require arbitration."""
        token = run_id or self._active_semantic_run_id or self._metadata("semantic_scan_run_id") or ""
        with self._connect() as connection:
            pending = self._pending_objection_count(connection)
            status = "needs_arbitration" if pending else READY_STATUS
            reason = "pending_arbitration" if pending else "semantic_scan_completed"
            if not token or not self._commit_semantic_run_terminal(
                connection,
                token,
                semantic_status="completed",
                map_status=status,
                reason=reason,
            ):
                connection.execute(
                    "UPDATE metadata SET value = 'pending' "
                    "WHERE key = 'semantic_scan_status' AND value = 'running' "
                    "AND COALESCE((SELECT value FROM metadata WHERE key = 'semantic_scan_run_id'), '') = ?",
                    (token,),
                )
                return self.readiness()
        return self.readiness(status)

    def run_semantic_scan(self, *, executor: Callable[[ProjectPlanStore], None] | None = None) -> dict[str, Any]:
        """Run the semantic phase after structure scan; executor hook supports tests and failure injection."""
        scan_status = self._metadata("scan_status") or "not_scanned"
        if scan_status == "failed":
            raise ConflictError(
                resource="project_map",
                message="Semantic scan requires structure_ready state",
                error_code="semantic_scan_not_allowed",
                details={"scan_status": scan_status},
            )
        if scan_status not in ("structure_ready", "semantic_scanning"):
            raise ConflictError(
                resource="project_map",
                message="Semantic scan requires structure_ready or semantic_scanning state",
                error_code="semantic_scan_not_allowed",
                details={"scan_status": scan_status},
            )
        semantic_status = self._metadata("semantic_scan_status") or "pending"
        if semantic_status == "completed":
            return self.readiness()
        if semantic_status == "running":
            raise ConflictError(
                resource="project_map",
                message="Semantic scan already running for this project",
                error_code="semantic_scan_in_progress",
            )
        if not self._try_acquire_semantic_scan_lock():
            semantic_status = self._metadata("semantic_scan_status") or "pending"
            if semantic_status == "running":
                raise ConflictError(
                    resource="project_map",
                    message="Semantic scan already running for this project",
                    error_code="semantic_scan_in_progress",
                )
            if semantic_status == "completed":
                return self.readiness()
            raise ConflictError(
                resource="project_map",
                message="Could not acquire semantic scan lock",
                error_code="semantic_scan_lock_failed",
            )
        run_id = self._active_semantic_run_id or self._metadata("semantic_scan_run_id") or ""
        started = time.perf_counter()
        self._log("project_map_semantic_scan", "started")
        if not self._semantic_continuing_from_failure and not self._semantic_resuming_interrupted_run:
            with self._connect() as connection:
                self._set_metadata(connection, "semantic_scan_processed", "0")
                self._set_metadata(connection, "semantic_scan_routed", "0")
                self._set_metadata(connection, "semantic_scan_deferred", "0")
        total_processed = int(self._metadata("semantic_scan_processed") or "0")
        total_routed = int(self._metadata("semantic_scan_routed") or "0")
        total_deferred = int(self._metadata("semantic_scan_deferred") or "0")
        batch_index = 0
        try:
            if executor is None:
                while True:
                    batch_index += 1
                    batch_started = time.perf_counter()
                    batch = self._run_semantic_batch()
                    total_processed += int(batch["processed"])
                    total_routed += int(batch["routed"])
                    total_deferred += int(batch["deferred"])
                    with self._connect() as connection:
                        self._set_metadata(connection, "semantic_scan_processed", str(total_processed))
                        self._set_metadata(connection, "semantic_scan_routed", str(total_routed))
                        self._set_metadata(connection, "semantic_scan_deferred", str(total_deferred))
                        self._set_metadata(
                            connection,
                            "semantic_scan_remaining",
                            str(batch["remaining"]),
                        )
                    self._log(
                        "project_map_semantic_scan",
                        "batch",
                        duration_ms=self._elapsed_ms(batch_started),
                        detail={
                            "batch_index": batch_index,
                            "processed": batch["processed"],
                            "routed": batch["routed"],
                            "deferred": batch["deferred"],
                            "remaining": batch["remaining"],
                            "cumulative_processed": total_processed,
                            "cumulative_routed": total_routed,
                            "cumulative_deferred": total_deferred,
                        },
                    )
                    if int(batch["remaining"]) <= 0:
                        break
                result = self.mark_semantic_scan_completed(run_id=run_id)
            else:
                executor(self)
                remaining = int(self._metadata("semantic_scan_remaining") or "0")
                if remaining > 0:
                    with self._connect() as connection:
                        if run_id and self._commit_semantic_run_terminal(
                            connection,
                            run_id,
                            semantic_status="pending",
                            map_status="semantic_scanning",
                            reason="blind_spots_remaining",
                        ):
                            result = self.readiness("semantic_scanning")
                        else:
                            result = self.readiness()
                    self._log(
                        "project_map_semantic_scan",
                        "deferred",
                        duration_ms=self._elapsed_ms(started),
                        detail={"remaining_blind_spots": remaining},
                    )
                    return result
                result = self.mark_semantic_scan_completed(run_id=run_id)
            self._log(
                "project_map_semantic_scan",
                "completed",
                duration_ms=self._elapsed_ms(started),
                detail={
                    "scan_status": result["scan_status"],
                    "cumulative_processed": total_processed,
                    "cumulative_routed": total_routed,
                    "cumulative_deferred": total_deferred,
                },
            )
            return result
        except Exception as exc:
            remaining = 0
            committed = False
            with self._connect() as connection:
                remaining = self._open_blind_spot_count(connection)
                self._set_metadata(connection, "semantic_scan_remaining", str(remaining))
                if run_id:
                    committed = self._commit_semantic_run_terminal(
                        connection,
                        run_id,
                        semantic_status="failed",
                        map_status="semantic_scanning",
                        reason=type(exc).__name__,
                    )
                    if not committed:
                        connection.execute(
                            "UPDATE metadata SET value = 'pending' "
                            "WHERE key = 'semantic_scan_status' AND value = 'running' "
                            "AND COALESCE((SELECT value FROM metadata WHERE key = 'semantic_scan_run_id'), '') = ?",
                            (run_id,),
                        )
            if committed:
                self._log(
                    "project_map_semantic_scan",
                    "failed",
                    detail={
                        "error_code": type(exc).__name__,
                        "cumulative_processed": total_processed,
                        "cumulative_routed": total_routed,
                        "cumulative_deferred": total_deferred,
                        "remaining": remaining,
                        "batch_index": batch_index,
                    },
                    duration_ms=self._elapsed_ms(started),
                )
            raise

    def _try_acquire_semantic_scan_lock(self) -> bool:
        """Atomically claim pending semantic scan work; only one caller succeeds."""
        self._semantic_continuing_from_failure = False
        self._semantic_resuming_interrupted_run = False
        with self._connect() as connection:
            prior_semantic = self._metadata_from_connection(connection, "semantic_scan_status")
            interrupted = self._metadata_from_connection(connection, "semantic_scan_interrupted") == "1"
            existing_run_id = self._metadata_from_connection(connection, "semantic_scan_run_id") or ""
            cursor = connection.execute(
                "UPDATE metadata SET value = 'running' "
                "WHERE key = 'semantic_scan_status' "
                "AND value IN ('pending', 'not_started', 'failed') "
                "AND COALESCE((SELECT value FROM metadata WHERE key = 'scan_status'), '') "
                "IN ('structure_ready', 'semantic_scanning')"
            )
            if cursor.rowcount != 1:
                return False
            if prior_semantic == "failed" and existing_run_id:
                self._semantic_continuing_from_failure = True
                run_id = existing_run_id
            elif interrupted and existing_run_id:
                self._semantic_resuming_interrupted_run = True
                run_id = existing_run_id
                self._set_metadata(connection, "semantic_scan_interrupted", "0")
            else:
                run_id = self._allocate_semantic_run_id(connection)
                self._set_metadata(connection, "semantic_scan_run_id", run_id)
            self._active_semantic_run_id = run_id

        with self._connect() as connection:
            scan_cursor = connection.execute(
                "UPDATE metadata SET value = 'semantic_scanning' "
                "WHERE key = 'scan_status' "
                "AND value IN ('structure_ready', 'semantic_scanning')"
            )
            if scan_cursor.rowcount != 1:
                connection.execute(
                    "UPDATE metadata SET value = 'pending' "
                    "WHERE key = 'semantic_scan_status' AND value = 'running'"
                )
                self._active_semantic_run_id = None
                return False
            self._set_metadata(connection, "readiness_reason", "semantic_scan_started")
            self._record_change(
                connection,
                "project_map",
                self.project_id,
                "semantic_scanning",
                {"reason": "semantic_scan_started"},
            )
            return True

    def _run_semantic_batch(self) -> dict[str, Any]:
        from bridle.features.project_map.semantic_scan_service import SemanticScanService

        return SemanticScanService().run(self)

    def _default_semantic_scan(self, store: ProjectPlanStore) -> None:
        """Route open blind spots into reviewable annotations as the deterministic semantic phase."""
        store._run_semantic_batch()

    def _recover_interrupted_semantic_scan(self) -> None:
        """Crash recovery: a stale running marker becomes retryable pending."""
        if self._metadata("semantic_scan_status") == "running":
            with self._connect() as connection:
                self._set_metadata(connection, "semantic_scan_status", "pending")
                self._set_metadata(connection, "semantic_scan_interrupted", "1")

    def _maybe_run_semantic_scan(self) -> None:
        """Start or continue semantic scan while batches remain."""
        scan_status = self._metadata("scan_status") or "not_scanned"
        semantic_status = self._metadata("semantic_scan_status") or "not_started"
        if scan_status in ("structure_ready", "semantic_scanning") and semantic_status in (
            "pending",
            "not_started",
            "failed",
        ):
            self.run_semantic_scan()

    def readiness(self, status: str | None = None) -> dict[str, Any]:
        """Read map readiness; optional status input exits with chat/edit flags and reason."""
        scan_status = status or self._metadata("scan_status") or "not_scanned"
        reason = None if scan_status == READY_STATUS else self._metadata("readiness_reason") or scan_status
        return self._readiness_from_values(scan_status, reason)

    def record_semantic_annotation(
        self,
        *,
        source_id: str,
        summary: str,
        evidence: dict[str, Any],
        model: str,
        confidence: float,
        file_hash: str,
        status: str = "active",
    ) -> dict[str, Any]:
        """Persist one AI semantic note; status controls whether it is authoritative."""
        if status not in ("active", "pending", "rejected", "stale"):
            raise ValidationError(
                resource="semantic_annotation",
                message="Unsupported annotation status",
                details={"status": status},
            )
        annotation_id = f"annotation-{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO semantic_annotations("
                "id, source_id, summary, evidence, model, confidence, file_hash, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    annotation_id,
                    source_id,
                    summary,
                    json.dumps(evidence, ensure_ascii=False),
                    model,
                    float(confidence),
                    file_hash,
                    status,
                ),
            )
            self._record_change(
                connection,
                "semantic_annotation",
                annotation_id,
                "record",
                {"source_id": source_id, "model": model, "status": status},
            )
        self._log(
            "project_map_semantic_annotation",
            "completed",
            detail={"source_id": source_id, "status": status},
        )
        return {
            "id": annotation_id,
            "source_id": source_id,
            "summary": summary,
            "evidence": evidence,
            "model": model,
            "confidence": float(confidence),
            "file_hash": file_hash,
            "status": status,
        }

    def create_map_objection(
        self,
        *,
        objection_type: str,
        related_node_ids: list[str],
        evidence: dict[str, Any],
        suggested_resolution: dict[str, Any],
        annotation_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one AI objection; input exits with map held in arbitration state."""
        objection_id = f"objection-{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO map_objections("
                "id, objection_type, related_node_ids, evidence, suggested_resolution, annotation_id"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    objection_id,
                    objection_type,
                    json.dumps(related_node_ids, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(suggested_resolution, ensure_ascii=False),
                    annotation_id,
                ),
            )
            self._record_change(
                connection,
                "map_objection",
                objection_id,
                "create",
                {"objection_type": objection_type},
            )
            self._set_map_status(connection, "needs_arbitration", reason="pending_arbitration")
        self._log("project_map_objection", "created", detail={"objection_type": objection_type})
        return {
            "id": objection_id,
            "objection_type": objection_type,
            "related_node_ids": related_node_ids,
            "evidence": evidence,
            "suggested_resolution": suggested_resolution,
            "status": "pending",
        }

    def list_arbitration_items(self, *, include_resolved: bool = False) -> dict[str, Any]:
        """Read arbitration queue; flag input exits pending items by default."""
        where = "" if include_resolved else "WHERE status = 'pending'"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM map_objections {where} ORDER BY created_at, id"
            ).fetchall()
        return {"items": [self._objection_from_row(row) for row in rows]}

    def resolve_objection(
        self,
        objection_id: str,
        *,
        decision: str,
        resolution: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        """Resolve one objection; decision input exits with status and readiness recomputed."""
        result_id = f"arbitration-{uuid.uuid4().hex}"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM map_objections WHERE id = ?", (objection_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(resource="map_objection", details={"objection_id": objection_id})
            if str(row["status"]) != "pending":
                raise ConflictError(
                    resource="map_objection",
                    message="Objection already resolved",
                    error_code="objection_already_resolved",
                    details={"objection_id": objection_id, "status": row["status"]},
                )
            annotation_id = row["annotation_id"]
            if annotation_id and decision == "accepted":
                ann_row = connection.execute(
                    "SELECT sa.source_id, sa.file_hash, sa.status, ce.path "
                    "FROM semantic_annotations sa "
                    "JOIN code_entities ce ON ce.id = sa.source_id "
                    "WHERE sa.id = ?",
                    (annotation_id,),
                ).fetchone()
                if ann_row is None or str(ann_row["status"]) == "stale":
                    raise ConflictError(
                        resource="semantic_annotation",
                        message="Annotation is stale and cannot be accepted",
                        error_code="annotation_stale",
                        details={"annotation_id": annotation_id},
                    )
                file_path = str(ann_row["path"]).split("::", 1)[0]
                expected_hash = self._file_content_hash(file_path)
                if not expected_hash or expected_hash != str(ann_row["file_hash"]):
                    raise ConflictError(
                        resource="semantic_annotation",
                        message="Source file hash no longer matches annotation",
                        error_code="annotation_stale",
                        details={"annotation_id": annotation_id, "file_path": file_path},
                    )
            connection.execute(
                "UPDATE map_objections SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (objection_id,),
            )
            connection.execute(
                "INSERT INTO arbitration_results(id, objection_id, decision, resolution, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result_id,
                    objection_id,
                    decision,
                    json.dumps(resolution, ensure_ascii=False),
                    actor,
                ),
            )
            self._record_change(
                connection,
                "map_objection",
                objection_id,
                "resolve",
                {"decision": decision, "actor": actor},
            )
            if annotation_id:
                annotation_status = "active" if decision == "accepted" else "rejected"
                connection.execute(
                    "UPDATE semantic_annotations SET status = ? WHERE id = ?",
                    (annotation_status, annotation_id),
                )
                self._record_change(
                    connection,
                    "semantic_annotation",
                    str(annotation_id),
                    "arbitrate",
                    {"decision": decision, "status": annotation_status},
                )
            pending = self._pending_objection_count(connection)
            semantic_done = self._metadata_from_connection(connection, "semantic_scan_status") == "completed"
            if pending:
                self._set_map_status(connection, "needs_arbitration", reason="pending_arbitration")
            elif semantic_done:
                self._set_map_status(connection, READY_STATUS, reason="arbitration_completed")
            else:
                self._set_map_status(
                    connection,
                    "semantic_scanning",
                    reason="arbitration_completed_semantic_pending",
                )
        self._log("project_map_arbitration", "completed", detail={"objection_id": objection_id})
        return {"id": objection_id, "status": "resolved", "decision": decision, "result_id": result_id}

    def record_execution_refresh(
        self,
        *,
        execution_node_id: str,
        changed_paths: list[str],
        execution_summary: str,
        test_summary: str,
    ) -> dict[str, Any]:
        """Record execution completion; failures create runtime blind spots and bounded reindex."""
        feedback = self._runtime_feedback.process_failure(
            execution_summary=execution_summary,
            test_summary=test_summary,
            changed_paths=changed_paths,
        )
        refresh_paths = sorted(set(changed_paths) | set(feedback.refresh_paths))
        refresh = self.refresh_code_paths(refresh_paths)
        refresh_id = f"execution-refresh-{uuid.uuid4().hex}"
        with self._connect() as connection:
            for row in RuntimeFeedbackService.blind_spot_rows(
                feedback, file_paths=feedback.refresh_paths
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO map_blind_spots("
                    "id, kind, file_path, range, detail, source, status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        row["kind"],
                        row["file_path"],
                        row["range"],
                        row["detail"],
                        row["source"],
                        row["status"],
                    ),
                )
            connection.execute(
                "INSERT INTO execution_refreshes("
                "id, execution_node_id, changed_paths, refreshed_paths, execution_summary, test_summary"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    refresh_id,
                    execution_node_id,
                    json.dumps(changed_paths, ensure_ascii=False),
                    json.dumps(refresh["refreshed_paths"], ensure_ascii=False),
                    execution_summary,
                    test_summary,
                ),
            )
            self._record_change(
                connection,
                "execution_refresh",
                refresh_id,
                "record",
                {"execution_node_id": execution_node_id, "refreshed_paths": refresh["refreshed_paths"]},
            )
        self._log(
            "project_map_execution_refresh",
            "completed",
            node_id=execution_node_id,
            detail={"path_count": len(refresh["refreshed_paths"])},
        )
        return {
            "id": refresh_id,
            "execution_node_id": execution_node_id,
            "changed_paths": changed_paths,
            "refreshed_paths": refresh["refreshed_paths"],
            "execution_summary": execution_summary,
            "test_summary": test_summary,
            "runtime_blind_spots": feedback.blind_spot_ids,
            "reindex_attempts": feedback.reindex_attempts,
            "stopped_reason": feedback.stopped_reason,
        }

    def start_node(self, node_id: str) -> dict[str, Any]:
        """Atomically start an eligible node; node ID input exits running or node_not_runnable."""
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE plan_nodes SET status = 'running' "
                "WHERE id = ? AND archived = 0 AND status IN ('pending', 'ready') "
                "AND NOT EXISTS ("
                "SELECT 1 FROM plan_edges edge "
                "JOIN plan_nodes dependency ON dependency.id = edge.target_id "
                "WHERE edge.source_id = ? AND edge.kind = 'depends_on' "
                "AND dependency.status != 'completed'"
                ")",
                (node_id, node_id),
            )
            if result.rowcount == 0:
                existing = connection.execute(
                    "SELECT status FROM plan_nodes WHERE id = ? AND archived = 0", (node_id,),
                ).fetchone()
                if existing is None:
                    raise NotFoundError(resource="plan_node", details={"node_id": node_id})
                raise ConflictError(
                    resource="plan_node",
                    message="Node is not runnable from its current state or dependencies",
                    details={"node_id": node_id, "status": existing["status"]},
                    error_code="node_not_runnable",
                )
            self._record_change(connection, "plan_node", node_id, "status", {"status": "running"})
        self._log("project_node_status", "completed", node_id=node_id, detail={"status": "running"})
        return self.get_node(node_id)

    def get_node(self, node_id: str) -> dict[str, Any]:
        """Read one node by ID; input is an ID and output is its structured fields plus payload/deps."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM plan_nodes WHERE id = ? AND archived = 0", (node_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(resource="plan_node", details={"node_id": node_id})
            return self._row_to_node(connection, row)

    def overview(self) -> dict[str, Any]:
        """Summarize the active map; no input and output contains bounded root nodes and counts."""
        with self._connect() as connection:
            node_count = int(
                connection.execute("SELECT COUNT(*) FROM plan_nodes WHERE archived = 0").fetchone()[0]
            )
            code_count = int(connection.execute("SELECT COUNT(*) FROM code_entities").fetchone()[0])
            roots = connection.execute(
                "SELECT * FROM plan_nodes WHERE archived = 0 AND parent_id IS NULL "
                "ORDER BY node_order, id LIMIT 50"
            ).fetchall()
            scan_status = self._metadata_from_connection(connection, "scan_status") or "not_scanned"
            reason = None if scan_status == READY_STATUS else (
                self._metadata_from_connection(connection, "readiness_reason") or scan_status
            )
            return {
                "project_id": self.project_id,
                **self._readiness_from_values(scan_status, reason),
                "plan_node_count": node_count,
                "code_entity_count": code_count,
                "roots": [self._row_to_node(connection, row) for row in roots],
                "change_seq": self._latest_change_seq(connection),
            }

    def children(
        self,
        *,
        parent_id: str | None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Page direct children; parent/cursor/limit input returns stable ordered rows and next cursor."""
        page_limit = self._bounded_limit(limit)
        order_cursor, id_cursor = self._decode_order_cursor(cursor)
        where_parent = "parent_id IS NULL" if parent_id is None else "parent_id = ?"
        params: list[Any] = [] if parent_id is None else [parent_id]
        if cursor is not None:
            where_cursor = " AND (node_order > ? OR (node_order = ? AND id > ?))"
            params.extend([order_cursor, order_cursor, id_cursor])
        else:
            where_cursor = ""
        params.append(page_limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM plan_nodes WHERE archived = 0 AND {where_parent}{where_cursor} "
                "ORDER BY node_order, id LIMIT ?",
                params,
            ).fetchall()
            items = [self._row_to_node(connection, row) for row in rows[:page_limit]]
        next_cursor = None
        if len(rows) > page_limit and items:
            last = items[-1]
            next_cursor = self._encode_cursor([last["order"], last["id"]])
        return {"items": items, "next_cursor": next_cursor}

    def search(self, query: str, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Search node text; query/cursor/limit input returns a bounded ID-ordered page."""
        page_limit = self._bounded_limit(limit)
        id_cursor = self._decode_text_cursor(cursor)
        pattern = f"%{query.lower()}%"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_nodes WHERE archived = 0 AND id > ? "
                "AND (LOWER(id) LIKE ? OR LOWER(title) LIKE ? OR LOWER(goal) LIKE ? "
                "OR LOWER(payload) LIKE ?) ORDER BY id LIMIT ?",
                (id_cursor, pattern, pattern, pattern, pattern, page_limit + 1),
            ).fetchall()
            items = [self._row_to_node(connection, row) for row in rows[:page_limit]]
        next_cursor = self._encode_cursor([items[-1]["id"]]) if len(rows) > page_limit and items else None
        return {"items": items, "next_cursor": next_cursor}

    def subgraph(self, node_id: str, *, depth: int = 1, limit: int = 100) -> dict[str, Any]:
        """Read a bounded neighborhood; center/depth/limit input returns nodes and connecting edges."""
        bounded_depth = max(0, min(depth, MAX_SUBGRAPH_DEPTH))
        page_limit = self._bounded_limit(limit)
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM plan_nodes WHERE id = ? AND archived = 0", (node_id,),
            ).fetchone() is None:
                raise NotFoundError(resource="plan_node", details={"node_id": node_id})
            seen = {node_id}
            queue: deque[tuple[str, int]] = deque([(node_id, 0)])
            while queue and len(seen) < page_limit:
                current, level = queue.popleft()
                if level >= bounded_depth:
                    continue
                neighbor_rows = connection.execute(
                    "SELECT id FROM plan_nodes WHERE archived = 0 AND parent_id = ? "
                    "UNION SELECT parent_id FROM plan_nodes WHERE archived = 0 AND id = ? AND parent_id IS NOT NULL "
                    "UNION SELECT target_id FROM plan_edges WHERE source_id = ? "
                    "UNION SELECT source_id FROM plan_edges WHERE target_id = ?",
                    (current, current, current, current),
                ).fetchall()
                for neighbor_row in neighbor_rows:
                    neighbor = str(neighbor_row[0])
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    queue.append((neighbor, level + 1))
                    if len(seen) >= page_limit:
                        break
            node_rows = connection.execute(
                f"SELECT * FROM plan_nodes WHERE archived = 0 AND id IN ({','.join('?' for _ in seen)})",
                tuple(sorted(seen)),
            ).fetchall()
            edge_rows = connection.execute(
                f"SELECT source_id, target_id, kind FROM plan_edges "
                f"WHERE source_id IN ({','.join('?' for _ in seen)}) "
                f"AND target_id IN ({','.join('?' for _ in seen)})",
                (*sorted(seen), *sorted(seen)),
            ).fetchall()
            return {
                "nodes": [self._row_to_node(connection, row) for row in node_rows],
                "edges": [dict(row) for row in edge_rows],
                "truncated": len(seen) >= page_limit and bool(queue),
            }

    def changes(self, *, after_seq: int, limit: int = 100) -> dict[str, Any]:
        """Read incremental events; sequence/limit input returns ordered events and latest sequence."""
        page_limit = self._bounded_limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM change_events WHERE change_seq > ? ORDER BY change_seq LIMIT ?",
                (max(after_seq, 0), page_limit),
            ).fetchall()
        items = [self._event_from_row(row) for row in rows]
        return {"items": items, "last_seq": items[-1]["change_seq"] if items else after_seq}

    def list_code_entities(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Page code entities; cursor/limit input returns stable path-ordered map rows."""
        page_limit = self._bounded_limit(limit)
        path_cursor, id_cursor = self._decode_path_cursor(cursor)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM code_entities WHERE path > ? OR (path = ? AND id > ?) "
                "ORDER BY path, id LIMIT ?",
                (path_cursor, path_cursor, id_cursor, page_limit + 1),
            ).fetchall()
        items = [self._code_entity_from_row(row) for row in rows[:page_limit]]
        next_cursor = None
        if len(rows) > page_limit and items:
            next_cursor = self._encode_cursor([items[-1]["path"], items[-1]["id"]])
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }

    def list_code_relations(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """Page structural code relations (imports/calls/inherits/contains)."""
        page_limit = self._bounded_limit(limit)
        row_cursor = int(cursor) if cursor and str(cursor).isdigit() else 0
        kind_clause = ""
        params: list[Any] = [row_cursor]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            kind_clause = f" AND kind IN ({placeholders})"
            params.extend(kinds)
        params.append(page_limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid, source_id, target_id, kind, payload FROM code_relations "
                f"WHERE rowid > ?{kind_clause} ORDER BY rowid LIMIT ?",
                params,
            ).fetchall()
        items = [
            {
                "source_id": str(row[1]),
                "target_id": str(row[2]),
                "kind": str(row[3]),
                "payload": json.loads(row[4] or "{}"),
            }
            for row in rows[:page_limit]
        ]
        next_cursor = None
        if len(rows) > page_limit and items:
            next_cursor = str(rows[page_limit - 1][0])
        return {"items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None}

    def list_semantic_annotations(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Page AI semantic annotations for the semantic map layer."""
        page_limit = self._bounded_limit(limit)
        id_cursor = cursor or ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, source_id, summary, evidence, model, confidence, file_hash, status "
                "FROM semantic_annotations WHERE id > ? ORDER BY id LIMIT ?",
                (id_cursor, page_limit + 1),
            ).fetchall()
        items = [
            {
                "id": str(row[0]),
                "source_id": str(row[1]),
                "summary": str(row[2]),
                "evidence": json.loads(row[3] or "{}"),
                "model": str(row[4]),
                "confidence": float(row[5]),
                "file_hash": str(row[6]),
                "status": str(row[7]),
            }
            for row in rows[:page_limit]
        ]
        next_cursor = items[-1]["id"] if len(rows) > page_limit and items else None
        return {"items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None}

    def semantic_scan_status(self) -> dict[str, Any]:
        return {
            "semantic_scan_status": self._metadata("semantic_scan_status") or "pending",
            "scan_status": self._metadata("scan_status") or "not_scanned",
        }

    def rescan_structure_only(self) -> dict[str, Any]:
        """Run rescan without completing semantic phase; for tests and explicit workflows."""
        return self.rescan()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one short SQLite transaction; no input and output is a row-aware connection."""
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _normalize_relative_path(raw_path: str) -> str:
        """Validate one changed path; raw input returns normalized POSIX workspace-relative text."""
        normalized = str(raw_path).replace("\\", "/").strip("/")
        parts = [part for part in normalized.split("/") if part and part != "."]
        if (
            not parts
            or ".." in parts
            or Path(raw_path).is_absolute()
            or (parts and ":" in parts[0])
            or not WorkspaceOverviewService._map_path_allowed("/".join(parts))
        ):
            raise ValidationError(
                resource="code_map",
                message="Changed path must stay inside the project",
                details={"path": raw_path},
            )
        return "/".join(parts)

    def _initialize_metadata(self, connection: sqlite3.Connection) -> None:
        """Validate/write metadata; connection input exits with schema/project identity persisted."""
        stored_kind = connection.execute(
            "SELECT value FROM metadata WHERE key = 'store_kind'"
        ).fetchone()
        if stored_kind is not None and stored_kind[0] != "plan":
            raise ValidationError(
                resource="project_map",
                message="plan.db has the wrong store kind",
                details={"expected": "plan", "actual": stored_kind[0]},
            )
        stored_project = connection.execute(
            "SELECT value FROM metadata WHERE key = 'project_id'"
        ).fetchone()
        if stored_project is not None and stored_project[0] != self.project_id:
            raise ValidationError(
                resource="project_map",
                message="plan.db belongs to another project",
                details={"expected": self.project_id, "actual": stored_project[0]},
            )
        self._set_metadata(connection, "schema_version", SCHEMA_VERSION)
        self._set_metadata(connection, "store_kind", "plan")
        self._set_metadata(connection, "project_id", self.project_id)
        if stored_project is None:
            self._set_metadata(connection, "scan_status", "not_scanned")
            self._set_metadata(connection, "semantic_scan_status", "not_started")
            self._set_metadata(connection, "readiness_reason", "not_scanned")

    def _validate_existing_metadata(self) -> None:
        """Reject a foreign existing DB through a read-only connection before plan DDL runs."""
        if not self.database_path.is_file():
            return
        connection = sqlite3.connect(f"{self.database_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            metadata_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
            ).fetchone()
            if metadata_exists is None:
                return
            stored_kind = connection.execute(
                "SELECT value FROM metadata WHERE key = 'store_kind'"
            ).fetchone()
            if stored_kind is not None and stored_kind[0] != "plan":
                raise ValidationError(
                    resource="project_map",
                    message="plan.db has the wrong store kind",
                    details={"expected": "plan", "actual": stored_kind[0]},
                )
            stored_project = connection.execute(
                "SELECT value FROM metadata WHERE key = 'project_id'"
            ).fetchone()
            if stored_project is not None and stored_project[0] != self.project_id:
                raise ValidationError(
                    resource="project_map",
                    message="plan.db belongs to another project",
                    details={"expected": self.project_id, "actual": stored_project[0]},
                )
        finally:
            connection.close()

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """Apply additive schema migrations for existing plan.db files."""
        objection_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(map_objections)").fetchall()
        }
        if "annotation_id" not in objection_columns:
            connection.execute("ALTER TABLE map_objections ADD COLUMN annotation_id TEXT")
        receipt_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(child_result_receipts)"
            ).fetchall()
        }
        if "result_json" not in receipt_columns:
            connection.execute(
                "ALTER TABLE child_result_receipts "
                "ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'"
            )
        workflow_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(modification_workflows)"
            ).fetchall()
        }
        if "test_contract_version" not in workflow_columns:
            connection.execute(
                "ALTER TABLE modification_workflows ADD COLUMN test_contract_version TEXT"
            )
        verification_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(verification_runs)"
            ).fetchall()
        }
        if verification_columns and {
            "next_retry_at",
            "max_attempts",
            "terminal_reason",
        } - verification_columns:
            connection.execute(
                "ALTER TABLE verification_runs RENAME TO verification_runs_v6"
            )
            connection.execute("DROP INDEX IF EXISTS ix_verification_runs_recovery")
            connection.execute("DROP INDEX IF EXISTS ix_verification_runs_node_sequence")
            connection.execute(
                """
                CREATE TABLE verification_runs (
                    sequence         INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id           TEXT NOT NULL UNIQUE,
                    node_id          TEXT NOT NULL,
                    phase            TEXT NOT NULL CHECK(phase IN ('red', 'final')),
                    source_revision  INTEGER NOT NULL,
                    contract_version TEXT NOT NULL,
                    candidate_id     TEXT,
                    state            TEXT NOT NULL CHECK(
                        state IN ('queued', 'running', 'completed', 'failed')
                    ),
                    attempt          INTEGER NOT NULL DEFAULT 0,
                    next_retry_at    REAL,
                    max_attempts     INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
                    terminal_reason  TEXT,
                    lease_token      TEXT,
                    lease_expires_at REAL,
                    outcome          TEXT,
                    error_code       TEXT,
                    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at     TEXT,
                    UNIQUE(node_id, phase, source_revision, contract_version)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO verification_runs(
                    sequence, run_id, node_id, phase, source_revision,
                    contract_version, candidate_id, state, attempt,
                    lease_token, lease_expires_at, outcome, error_code,
                    created_at, updated_at, completed_at
                )
                SELECT
                    sequence, run_id, node_id, phase, source_revision,
                    contract_version, candidate_id, state, attempt,
                    lease_token, lease_expires_at, outcome, error_code,
                    created_at, updated_at, completed_at
                FROM verification_runs_v6
                """
            )
            connection.execute("DROP TABLE verification_runs_v6")
            connection.execute(
                "CREATE INDEX ix_verification_runs_recovery ON verification_runs("
                "state, next_retry_at, lease_expires_at, sequence)"
            )
            connection.execute(
                "CREATE INDEX ix_verification_runs_node_sequence ON "
                "verification_runs(node_id, sequence)"
            )
        verification_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(verification_runs)"
            ).fetchall()
        }
        if "next_retry_at" in verification_columns:
            connection.execute("DROP INDEX IF EXISTS ix_verification_runs_recovery")
            connection.execute(
                "CREATE INDEX ix_verification_runs_recovery ON verification_runs("
                "state, next_retry_at, lease_expires_at, sequence)"
            )

    @staticmethod
    def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
        """Upsert one metadata value; connection/key/value input has a committed transaction side effect."""
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _metadata(self, key: str) -> str | None:
        """Read one metadata value; key input returns text or None."""
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _metadata_from_connection(connection: sqlite3.Connection, key: str) -> str | None:
        """Read metadata inside a caller-owned transaction."""
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _readiness_from_values(scan_status: str, reason: str | None) -> dict[str, Any]:
        """Build public readiness fields from already-read metadata values."""
        can_use_main = scan_status == READY_STATUS
        return {
            "scan_status": scan_status,
            "can_chat": can_use_main,
            "can_edit_plan": can_use_main,
            "readiness_reason": None if can_use_main else reason,
        }

    def _assert_map_ready(self, connection: sqlite3.Connection) -> None:
        """Reject plan mutation before map readiness; connection input exits or raises conflict."""
        status = self._metadata_from_connection(connection, "scan_status") or "not_scanned"
        if status == READY_STATUS:
            return
        reason = self._metadata_from_connection(connection, "readiness_reason") or status
        raise ConflictError(
            resource="project_map",
            message="Project map is not ready",
            error_code="project_map_not_ready",
            details={
                "scan_status": status,
                "can_chat": False,
                "can_edit_plan": False,
                "readiness_reason": reason,
            },
        )

    def _allocate_semantic_run_id(self, connection: sqlite3.Connection) -> str:
        """Allocate a globally monotonic semantic run id that is never reused."""
        current = int(self._metadata_from_connection(connection, "semantic_scan_run_seq") or "0")
        next_id = current + 1
        self._set_metadata(connection, "semantic_scan_run_seq", str(next_id))
        return str(next_id)

    @staticmethod
    def _open_blind_spot_count(connection: sqlite3.Connection) -> int:
        """Count open blind spots inside one transaction."""
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM map_blind_spots WHERE status = 'open'"
            ).fetchone()[0]
        )

    def _commit_semantic_run_terminal(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        semantic_status: str,
        map_status: str | None,
        reason: str,
    ) -> bool:
        """Commit semantic/map terminal states only when this run still owns the token."""
        scan_guard = (
            "AND COALESCE((SELECT value FROM metadata WHERE key = 'scan_status'), '') = 'semantic_scanning'"
            if map_status is not None
            else ""
        )
        cursor = connection.execute(
            f"""
            UPDATE metadata SET value = ?
            WHERE key = 'semantic_scan_status' AND value = 'running'
            AND COALESCE((SELECT value FROM metadata WHERE key = 'semantic_scan_run_id'), '') = ?
            {scan_guard}
            """,
            (semantic_status, run_id),
        )
        if cursor.rowcount != 1:
            return False
        if map_status is not None:
            self._set_metadata(connection, "scan_status", map_status)
            self._set_metadata(connection, "readiness_reason", "" if map_status == READY_STATUS else reason)
            self._record_change(
                connection,
                "project_map",
                self.project_id,
                map_status,
                {"reason": reason},
            )
        return True

    def _cas_commit_semantic_map_status(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        status: str,
        *,
        reason: str,
    ) -> bool:
        """Commit map terminal status only when this semantic run still owns the scan."""
        cursor = connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'scan_status' "
            "AND value = 'semantic_scanning' "
            "AND COALESCE((SELECT value FROM metadata WHERE key = 'semantic_scan_run_id'), '') = ?",
            (status, run_id),
        )
        if cursor.rowcount != 1:
            return False
        self._set_metadata(connection, "readiness_reason", "" if status == READY_STATUS else reason)
        self._record_change(
            connection,
            "project_map",
            self.project_id,
            status,
            {"reason": reason},
        )
        return True

    def _set_map_status(self, connection: sqlite3.Connection, status: str, *, reason: str) -> None:
        """Persist status and an event; connection/status input exits as map metadata."""
        self._set_metadata(connection, "scan_status", status)
        self._set_metadata(connection, "readiness_reason", "" if status == READY_STATUS else reason)
        self._record_change(
            connection,
            "project_map",
            self.project_id,
            status,
            {"reason": reason},
        )

    @staticmethod
    def _pending_objection_count(connection: sqlite3.Connection) -> int:
        """Count unresolved objections inside one transaction."""
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM map_objections WHERE status = 'pending'"
            ).fetchone()[0]
        )

    def _replace_code_map(
        self,
        connection: sqlite3.Connection,
        entities: list[dict],
        relations: list[dict] | None = None,
    ) -> None:
        """Replace explicit scan results; connection/entities/relations input rewrites code-map tables."""
        connection.execute("DELETE FROM code_relations")
        connection.execute("DELETE FROM code_entities")
        for entity in entities:
            connection.execute(
                "INSERT INTO code_entities(id, path, kind, name, parent_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entity["id"],
                    entity["path"],
                    entity["kind"],
                    entity["name"],
                    entity["parent_id"],
                    json.dumps(entity.get("payload", {}), ensure_ascii=False),
                ),
            )
            # Test files are registered for visibility but never connect edges (not even contains).
            if entity["parent_id"] is not None and entity["kind"] != "test":
                connection.execute(
                    "INSERT OR IGNORE INTO code_relations(source_id, target_id, kind) "
                    "VALUES (?, ?, 'contains')",
                    (entity["parent_id"], entity["id"]),
                )
        for relation in relations or []:
            self._insert_relation(connection, relation)

    @staticmethod
    def _insert_relation(connection: sqlite3.Connection, relation: dict) -> None:
        """Insert one non-contains code relation (imports/etc.) with its payload, idempotently."""
        connection.execute(
            "INSERT OR IGNORE INTO code_relations(source_id, target_id, kind, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                relation["source_id"],
                relation["target_id"],
                relation["kind"],
                json.dumps(relation.get("payload", {}), ensure_ascii=False),
            ),
        )

    def _collect_blind_spots(self, rel_paths: list[str], nontest_files: set[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for rel_path in rel_paths:
            if rel_path not in nontest_files:
                continue
            target = self.project_root.joinpath(*rel_path.split("/"))
            if not target.is_file():
                continue
            result = self._blind_spots.scan_file(target, rel_path, nontest_files=nontest_files)
            for spot in result.spots:
                row = BlindSpotDetector.to_row(spot)
                rows.append(row)
        return rows

    @staticmethod
    def _replace_scip_data(connection: sqlite3.Connection, scip) -> None:
        connection.execute("DELETE FROM code_occurrences")
        connection.execute("DELETE FROM code_symbols")
        ProjectPlanStore._upsert_scip_data(connection, scip)

    @staticmethod
    def _upsert_scip_data(connection: sqlite3.Connection, scip) -> None:
        for symbol in scip.symbols:
            connection.execute(
                "INSERT OR REPLACE INTO code_symbols(moniker, def_entity_id, kind, display_name) "
                "VALUES (?, ?, ?, ?)",
                (symbol["moniker"], symbol.get("def_entity_id"), symbol.get("kind"), symbol.get("display_name")),
            )
        for occ in scip.occurrences:
            connection.execute(
                "INSERT INTO code_occurrences(file_path, moniker, role, range) VALUES (?, ?, ?, ?)",
                (
                    occ["file_path"],
                    occ["moniker"],
                    occ["role"],
                    json.dumps(occ.get("range", {}), ensure_ascii=False),
                ),
            )

    @staticmethod
    def _replace_static_blind_spots(connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
        connection.execute("DELETE FROM map_blind_spots WHERE source = 'static'")
        for row in rows:
            connection.execute(
                "INSERT INTO map_blind_spots(id, kind, file_path, range, detail, source, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["kind"],
                    row["file_path"],
                    row["range"],
                    row["detail"],
                    row["source"],
                    row["status"],
                ),
            )

    @staticmethod
    def _upsert_blind_spots_for_files(
        connection: sqlite3.Connection,
        rel_paths: list[str],
        rows: list[dict[str, Any]],
    ) -> None:
        for rel_path in rel_paths:
            connection.execute(
                "DELETE FROM map_blind_spots WHERE source = 'static' AND file_path = ?",
                (rel_path,),
            )
        for row in rows:
            connection.execute(
                "INSERT INTO map_blind_spots(id, kind, file_path, range, detail, source, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["kind"],
                    row["file_path"],
                    row["range"],
                    row["detail"],
                    row["source"],
                    row["status"],
                ),
            )

    def map_get_node(self, entity_id: str, *, mapping_seed: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            if mapping_seed:
                self._map_query.require_blind_spot_seed(connection, mapping_seed)
                self._map_query.assert_entity_in_seed_scope(connection, mapping_seed, entity_id)
            return self._map_query.get_node(connection, entity_id)

    def map_neighbors(
        self,
        entity_id: str,
        *,
        kinds: list[str] | None = None,
        max_nodes: int = 50,
        mapping_seed: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            if mapping_seed:
                self._map_query.require_blind_spot_seed(connection, mapping_seed)
                self._map_query.assert_entity_in_seed_scope(connection, mapping_seed, entity_id)
            result = self._map_query.neighbors(connection, entity_id, kinds=kinds, max_nodes=max_nodes)
            if mapping_seed:
                allowed = self._map_query.seed_allowed_entity_ids(connection, mapping_seed)
                result["items"] = [item for item in result["items"] if item["id"] in allowed]
            return result

    def map_subgraph(
        self,
        entity_id: str,
        *,
        depth: int = 1,
        max_nodes: int = 50,
        kinds: list[str] | None = None,
        mapping_seed: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            if mapping_seed:
                self._map_query.require_blind_spot_seed(connection, mapping_seed)
                self._map_query.assert_entity_in_seed_scope(connection, mapping_seed, entity_id)
            result = self._map_query.subgraph(
                connection, entity_id, depth=depth, max_nodes=max_nodes, kinds=kinds
            )
            if mapping_seed:
                allowed = self._map_query.seed_allowed_entity_ids(connection, mapping_seed)
                result["nodes"] = [node for node in result["nodes"] if node["id"] in allowed]
                result["edges"] = [
                    edge
                    for edge in result["edges"]
                    if edge["source_id"] in allowed and edge["target_id"] in allowed
                ]
            return result

    def map_read_span(
        self,
        entity_id: str,
        *,
        max_tokens: int = 8000,
        mapping_seed: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            if mapping_seed:
                self._map_query.require_blind_spot_seed(connection, mapping_seed)
                self._map_query.assert_entity_in_seed_scope(connection, mapping_seed, entity_id)
            return self._map_query.read_span(connection, entity_id, max_tokens=max_tokens)

    def map_blind_spots(
        self,
        *,
        seed_id: str | None = None,
        status: str = "open",
        max_nodes: int = 50,
        require_seed: bool = False,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            if require_seed:
                if not seed_id:
                    raise ValidationError(
                        resource="map_blind_spot",
                        message="Mapping queries require an open blind spot seed",
                    )
                self._map_query.require_blind_spot_seed(connection, seed_id)
            return self._map_query.blind_spots(
                connection, seed_id=seed_id, status=status, max_nodes=max_nodes
            )

    def path_slice(self, rel_path: str) -> dict[str, Any]:
        """Return entities and relations scoped to one file path for incremental UI refresh."""
        normalized = self._normalize_relative_path(rel_path)
        with self._connect() as connection:
            entity_rows = connection.execute(
                "SELECT * FROM code_entities WHERE path = ? OR path LIKE ? ORDER BY path, id",
                (normalized, f"{normalized}::%"),
            ).fetchall()
            entities = [self._code_entity_from_row(row) for row in entity_rows]
            entity_ids = [entity["id"] for entity in entities]
            relations: list[dict[str, Any]] = []
            if entity_ids:
                placeholders = ",".join("?" for _ in entity_ids)
                rel_rows = connection.execute(
                    f"SELECT source_id, target_id, kind, payload FROM code_relations "
                    f"WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                    (*entity_ids, *entity_ids),
                ).fetchall()
                relations = [
                    {
                        "source_id": str(row[0]),
                        "target_id": str(row[1]),
                        "kind": str(row[2]),
                        "payload": json.loads(row[3] or "{}"),
                    }
                    for row in rel_rows
                ]
            blind_rows = connection.execute(
                "SELECT id, kind, file_path, range, detail, source, status "
                "FROM map_blind_spots WHERE file_path = ?",
                (normalized,),
            ).fetchall()
            blind_spots = [
                {
                    "id": str(row[0]),
                    "kind": str(row[1]),
                    "file_path": str(row[2]),
                    "range": json.loads(row[3] or "null") if row[3] else None,
                    "detail": json.loads(row[4] or "{}"),
                    "source": str(row[5]),
                    "status": str(row[6]),
                }
                for row in blind_rows
            ]
        return {"path": normalized, "entities": entities, "relations": relations, "blind_spots": blind_spots}

    def _route_blind_spot_to_review(
        self,
        connection: sqlite3.Connection,
        *,
        spot_id: str,
        source_id: str,
        file_path: str,
        summary: str,
        evidence: dict[str, Any],
    ) -> None:
        """Atomically route one blind spot into a pending annotation and arbitration item."""
        file_hash = self._file_content_hash(file_path)
        annotation_id = f"annotation-{uuid.uuid4().hex}"
        objection_id = f"objection-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO semantic_annotations("
            "id, source_id, summary, evidence, model, confidence, file_hash, status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                annotation_id,
                source_id,
                summary,
                json.dumps(evidence, ensure_ascii=False),
                "semantic_scan",
                0.35,
                file_hash,
                "pending",
            ),
        )
        connection.execute(
            "INSERT INTO map_objections("
            "id, objection_type, related_node_ids, evidence, suggested_resolution, annotation_id"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                objection_id,
                "semantic_annotation",
                json.dumps([source_id], ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                json.dumps({"summary": summary, "confidence": 0.35}, ensure_ascii=False),
                annotation_id,
            ),
        )
        connection.execute(
            "UPDATE map_blind_spots SET status = 'routed' WHERE id = ?",
            (spot_id,),
        )
        self._record_change(
            connection,
            "semantic_annotation",
            annotation_id,
            "record",
            {"source_id": source_id, "model": "semantic_scan", "status": "pending"},
        )
        self._record_change(
            connection,
            "map_objection",
            objection_id,
            "create",
            {"objection_type": "semantic_annotation", "blind_spot_id": spot_id},
        )

    @staticmethod
    def _collect_annotation_snapshots(
        connection: sqlite3.Connection,
        rel_paths: list[str],
    ) -> list[dict[str, str]]:
        snapshots: list[dict[str, str]] = []
        for rel_path in rel_paths:
            rows = connection.execute(
                "SELECT sa.id, sa.source_id, sa.file_hash FROM semantic_annotations sa "
                "JOIN code_entities ce ON ce.id = sa.source_id "
                "WHERE (ce.path = ? OR ce.path LIKE ?) AND sa.status IN ('active', 'pending')",
                (rel_path, f"{rel_path}::%"),
            ).fetchall()
            for row in rows:
                snapshots.append(
                    {
                        "id": str(row["id"]),
                        "source_id": str(row["source_id"]),
                        "file_hash": str(row["file_hash"]),
                    }
                )
        return snapshots

    def _reconcile_annotations_after_refresh(
        self,
        connection: sqlite3.Connection,
        snapshots: list[dict[str, str]],
    ) -> None:
        for snap in snapshots:
            row = connection.execute(
                "SELECT status FROM semantic_annotations WHERE id = ?",
                (snap["id"],),
            ).fetchone()
            if row is None or str(row["status"]) not in ("active", "pending"):
                continue
            source = connection.execute(
                "SELECT path FROM code_entities WHERE id = ?",
                (snap["source_id"],),
            ).fetchone()
            if source is None:
                self._set_annotation_stale(connection, snap["id"])
                continue
            file_path = str(source[0]).split("::", 1)[0]
            current_hash = self._file_content_hash(file_path)
            if not current_hash or current_hash != snap["file_hash"]:
                self._set_annotation_stale(connection, snap["id"])

    def _set_annotation_stale(self, connection: sqlite3.Connection, annotation_id: str) -> None:
        updated = connection.execute(
            "UPDATE semantic_annotations SET status = 'stale' "
            "WHERE id = ? AND status IN ('active', 'pending')",
            (annotation_id,),
        )
        if updated.rowcount == 0:
            return
        connection.execute(
            "UPDATE map_objections SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP "
            "WHERE annotation_id = ? AND status = 'pending'",
            (annotation_id,),
        )
        self._record_change(
            connection,
            "semantic_annotation",
            annotation_id,
            "stale",
            {},
        )

    def _file_content_hash(self, file_path: str) -> str:
        target = self.project_root.joinpath(*file_path.split("/"))
        if not target.is_file():
            return ""
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def propose_semantic_annotation(
        self,
        *,
        source_id: str,
        summary: str,
        evidence: dict[str, Any],
        model: str,
        confidence: float,
        file_hash: str,
        risk: str = "low",
        mapping_seed: str | None = None,
    ) -> dict[str, Any]:
        """Record semantic annotation; high-confidence low-risk auto-adopts, else queues objection."""
        if not (0.0 <= float(confidence) <= 1.0):
            raise ValidationError(
                resource="semantic_annotation",
                message="Confidence must be between 0 and 1",
                details={"confidence": confidence},
            )
        if risk not in SUPPORTED_RISKS:
            raise ValidationError(
                resource="semantic_annotation",
                message="Unsupported risk level",
                details={"risk": risk},
            )
        decision = ModifyLoopService.propose_annotation_decision(confidence=confidence, risk=risk)
        initial_status = "active" if decision == "auto_adopt" else "pending"
        annotation_id = f"annotation-{uuid.uuid4().hex}"
        objection_id: str | None = None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT path FROM code_entities WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(
                    resource="semantic_annotation",
                    message="Annotation source entity not found",
                    details={"source_id": source_id},
                )
            file_path = str(row[0]).split("::", 1)[0]
            expected_hash = self._file_content_hash(file_path)
            if not file_hash or file_hash != expected_hash:
                raise ValidationError(
                    resource="semantic_annotation",
                    message="File hash does not match current source content",
                    details={"source_id": source_id},
                )
            if mapping_seed:
                self._map_query.require_blind_spot_seed(connection, mapping_seed)
                self._map_query.assert_entity_in_seed_scope(connection, mapping_seed, source_id)
            connection.execute(
                "INSERT INTO semantic_annotations("
                "id, source_id, summary, evidence, model, confidence, file_hash, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    annotation_id,
                    source_id,
                    summary,
                    json.dumps(evidence, ensure_ascii=False),
                    model,
                    float(confidence),
                    file_hash,
                    initial_status,
                ),
            )
            self._record_change(
                connection,
                "semantic_annotation",
                annotation_id,
                "record",
                {"source_id": source_id, "model": model, "status": initial_status},
            )
            if decision == "objection":
                objection_id = f"objection-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO map_objections("
                    "id, objection_type, related_node_ids, evidence, suggested_resolution, annotation_id"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        objection_id,
                        "semantic_annotation",
                        json.dumps([source_id], ensure_ascii=False),
                        json.dumps(evidence, ensure_ascii=False),
                        json.dumps({"summary": summary, "confidence": confidence}, ensure_ascii=False),
                        annotation_id,
                    ),
                )
                self._record_change(
                    connection,
                    "map_objection",
                    objection_id,
                    "create",
                    {"objection_type": "semantic_annotation"},
                )
                self._set_map_status(connection, "needs_arbitration", reason="pending_arbitration")
        annotation: dict[str, Any] = {
            "id": annotation_id,
            "source_id": source_id,
            "summary": summary,
            "evidence": evidence,
            "model": model,
            "confidence": float(confidence),
            "file_hash": file_hash,
            "status": initial_status,
        }
        if objection_id is not None:
            annotation["objection_id"] = objection_id
            annotation["decision"] = "objection"
        else:
            annotation["decision"] = "auto_adopt"
        self._log(
            "project_map_semantic_annotation",
            "completed",
            detail={"source_id": source_id, "status": initial_status},
        )
        return annotation

    def list_boundary_conflicts(self, *, limit: int = 10) -> dict[str, Any]:
        with self._connect() as connection:
            items = self._boundary.list_boundary_conflicts(connection, limit=limit)
            debt = self._boundary.debt_node_ids(connection)
            return {"items": items, "debt_nodes": debt}

    def cluster_modules(self) -> dict[str, Any]:
        with self._connect() as connection:
            return {"modules": self._boundary.cluster_modules(connection)}

    def refresh_semantic_map_candidates(self) -> dict[str, Any]:
        """Regenerate deterministic semantic-map candidates from the current structure map."""
        with self._connect() as connection:
            return self._refresh_semantic_map_candidates(connection, reason="manual_refresh")

    def list_module_candidates(self, *, status: str | None = None, include_files: bool = True) -> dict[str, Any]:
        """List module candidates; confirmed rows are eligible execution boundaries."""
        with self._connect() as connection:
            params: list[Any] = []
            where = ""
            if status:
                where = "WHERE status = ?"
                params.append(status)
            rows = connection.execute(
                "SELECT * FROM module_candidates "
                f"{where} ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, module_id",
                params,
            ).fetchall()
            items = [self._module_candidate_from_row(row) for row in rows]
            if include_files:
                for item in items:
                    item["files"] = self._module_candidate_files(connection, item["id"])
            return {"items": items}

    def set_module_candidate_status(self, candidate_id: str, *, status: str, actor: str = "human") -> dict[str, Any]:
        """Confirm or reject one module candidate; only confirmed candidates can drive execution boundaries."""
        if status not in ("confirmed", "rejected"):
            raise ValidationError(
                resource="module_candidate",
                message="Unsupported module candidate status",
                details={"status": status},
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM module_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(resource="module_candidate", message="Module candidate not found")
            confirmed_at = "CURRENT_TIMESTAMP" if status == "confirmed" else "NULL"
            connection.execute(
                f"UPDATE module_candidates SET status = ?, confirmed_at = {confirmed_at} WHERE id = ?",
                (status, candidate_id),
            )
            self._record_change(
                connection,
                "module_candidate",
                candidate_id,
                status,
                {"actor": actor, "module_id": row["module_id"]},
            )
            updated = connection.execute("SELECT * FROM module_candidates WHERE id = ?", (candidate_id,)).fetchone()
            item = self._module_candidate_from_row(updated)
            item["files"] = self._module_candidate_files(connection, candidate_id)
            return item

    def list_module_interface_candidates(self, *, status: str | None = None) -> dict[str, Any]:
        """List interface candidates and their generated mock files."""
        with self._connect() as connection:
            params: list[Any] = []
            where = ""
            if status:
                where = "WHERE status = ?"
                params.append(status)
            rows = connection.execute(
                "SELECT * FROM module_interface_candidates "
                f"{where} ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, "
                "from_module, to_module, symbol",
                params,
            ).fetchall()
            return {"items": [self._interface_candidate_from_row(row) for row in rows]}

    def set_module_interface_candidate_status(
        self,
        candidate_id: str,
        *,
        status: str,
        actor: str = "human",
    ) -> dict[str, Any]:
        """Confirm or reject one interface candidate; confirmation publishes module_interfaces."""
        if status not in ("confirmed", "rejected"):
            raise ValidationError(
                resource="module_interface_candidate",
                message="Unsupported interface candidate status",
                details={"status": status},
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM module_interface_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(resource="module_interface_candidate", message="Interface candidate not found")
            item = self._interface_candidate_from_row(row)
            if status == "confirmed":
                mock_hash = self._file_content_hash(item["mock_file_path"])
                if not mock_hash or mock_hash != item["mock_hash"]:
                    raise ConflictError(
                        resource="module_interface_candidate",
                        message="Interface mock artifact is stale",
                        error_code="interface_mock_stale",
                        details={"candidate_id": candidate_id, "mock_file_path": item["mock_file_path"]},
                    )
            confirmed_at = "CURRENT_TIMESTAMP" if status == "confirmed" else "NULL"
            connection.execute(
                f"UPDATE module_interface_candidates SET status = ?, confirmed_at = {confirmed_at} WHERE id = ?",
                (status, candidate_id),
            )
            connection.execute(
                "UPDATE interface_mock_artifacts SET status = ? WHERE interface_candidate_id = ?",
                ("confirmed" if status == "confirmed" else "rejected", candidate_id),
            )
            if status == "confirmed":
                self._publish_confirmed_interface(connection, candidate_id)
            else:
                self._revoke_candidate_interface(connection, candidate_id)
            self._record_change(
                connection,
                "module_interface_candidate",
                candidate_id,
                status,
                {"actor": actor, "from_module": item["from_module"], "to_module": item["to_module"]},
            )
            updated = connection.execute(
                "SELECT * FROM module_interface_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            return self._interface_candidate_from_row(updated)

    def list_interface_mock_artifacts(self, *, status: str | None = None) -> dict[str, Any]:
        """List generated interface mock artifacts."""
        with self._connect() as connection:
            params: list[Any] = []
            where = ""
            if status:
                where = "WHERE status = ?"
                params.append(status)
            rows = connection.execute(
                f"SELECT * FROM interface_mock_artifacts {where} ORDER BY file_path",
                params,
            ).fetchall()
            return {"items": [self._mock_artifact_from_row(row) for row in rows]}

    def refresh_boundaries(self) -> dict[str, Any]:
        """Recompute co-change and metrics without changing ratified boundaries."""
        with self._connect() as connection:
            co_count = self._boundary.refresh_cochange(connection)
            metric_count = self._boundary.compute_metrics(
                connection, change_seq=self._latest_change_seq(connection)
            )
            semantic = self._refresh_semantic_map_candidates(connection, reason="boundary_refresh")
        return {"cochange_pairs": co_count, "metric_rows": metric_count, "semantic_map": semantic}

    def list_divergent_nodes(self) -> dict[str, Any]:
        with self._connect() as connection:
            return {"node_ids": ModifyLoopService.list_divergent_nodes(connection)}

    def get_active_test_contract(self, node_id: str) -> dict[str, Any] | None:
        """Read the only currently frozen test contract for one node."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT node_id, contract_version, snapshot, state, invalidation_reason, "
                "frozen_at, invalidated_at FROM test_contracts "
                "WHERE node_id = ? AND state = 'frozen'",
                (node_id,),
            ).fetchone()
        return None if row is None else self._test_contract_from_row(row)

    def get_test_contract(self, node_id: str, contract_version: str) -> dict[str, Any] | None:
        """Read one immutable test contract version, including invalidated versions."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT node_id, contract_version, snapshot, state, invalidation_reason, "
                "frozen_at, invalidated_at FROM test_contracts "
                "WHERE node_id = ? AND contract_version = ?",
                (node_id, contract_version),
            ).fetchone()
        return None if row is None else self._test_contract_from_row(row)

    def freeze_test_contract(
        self,
        node_id: str,
        *,
        contract_version: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable contract version without replacing its history."""
        started = time.perf_counter()
        serialized = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            if str(snapshot.get("contract_version") or "") != contract_version:
                raise ValidationError(
                    resource="test_contract",
                    message="Test contract snapshot version does not match",
                    details={"node_id": node_id, "contract_version": contract_version},
                )
            snapshot_payload = dict(snapshot)
            snapshot_payload.pop("contract_version", None)
            computed_version = hashlib.sha256(
                json.dumps(
                    snapshot_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if computed_version != contract_version:
                raise ValidationError(
                    resource="test_contract",
                    message="Test contract content hash does not match its version",
                    details={"node_id": node_id, "contract_version": contract_version},
                )
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT snapshot, state FROM test_contracts "
                    "WHERE node_id = ? AND contract_version = ?",
                    (node_id, contract_version),
                ).fetchone()
                if existing is not None:
                    if str(existing["snapshot"]) != serialized:
                        raise ConflictError(
                            resource="test_contract",
                            message="Test contract version is bound to another snapshot",
                            error_code="test_contract_version_conflict",
                            details={"node_id": node_id, "contract_version": contract_version},
                        )
                    if str(existing["state"]) != "frozen":
                        raise ConflictError(
                            resource="test_contract",
                            message="Invalidated test contract versions cannot be frozen again",
                            error_code="test_contract_version_invalidated",
                            details={"node_id": node_id, "contract_version": contract_version},
                        )
                    result = {
                        "node_id": node_id,
                        "contract_version": contract_version,
                        "state": "frozen",
                        "applied": False,
                    }
                    self._log(
                        "test_contract_freeze",
                        "reused",
                        node_id=node_id,
                        duration_ms=self._elapsed_ms(started),
                        detail=result,
                    )
                    return result
                active = connection.execute(
                    "SELECT contract_version FROM test_contracts "
                    "WHERE node_id = ? AND state = 'frozen'",
                    (node_id,),
                ).fetchone()
                if active is not None:
                    raise ConflictError(
                        resource="test_contract",
                        message="Another frozen test contract must be invalidated first",
                        error_code="test_contract_active_conflict",
                        details={
                            "node_id": node_id,
                            "contract_version": str(active["contract_version"]),
                        },
                    )
                connection.execute(
                    "INSERT INTO test_contracts"
                    "(node_id, contract_version, snapshot, state) VALUES (?, ?, ?, 'frozen')",
                    (node_id, contract_version, serialized),
                )
                self._record_change(
                    connection,
                    "test_contract",
                    f"{node_id}:{contract_version}",
                    "freeze",
                    {"node_id": node_id, "contract_version": contract_version},
                )
            result = {
                "node_id": node_id,
                "contract_version": contract_version,
                "state": "frozen",
                "applied": True,
            }
            self._log(
                "test_contract_freeze",
                "completed",
                node_id=node_id,
                duration_ms=self._elapsed_ms(started),
                detail=result,
            )
            return result
        except (ConflictError, ValidationError) as exc:
            self._log(
                "test_contract_freeze",
                "rejected",
                node_id=node_id,
                duration_ms=self._elapsed_ms(started),
                detail={
                    "contract_version": contract_version,
                    "error_code": exc.api_error.code,
                },
            )
            raise

    def invalidate_test_contract(
        self,
        node_id: str,
        *,
        contract_version: str,
        reason: str,
    ) -> dict[str, Any]:
        """Permanently invalidate one contract version while retaining its snapshot."""
        started = time.perf_counter()
        normalized_reason = str(reason).strip()
        try:
            if not normalized_reason:
                raise ValidationError(
                    resource="test_contract",
                    message="Test contract invalidation reason is required",
                )
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state, invalidation_reason FROM test_contracts "
                    "WHERE node_id = ? AND contract_version = ?",
                    (node_id, contract_version),
                ).fetchone()
                if row is None:
                    raise NotFoundError(
                        resource="test_contract",
                        message="Test contract not found",
                        details={"node_id": node_id, "contract_version": contract_version},
                    )
                if str(row["state"]) == "invalidated":
                    result = {
                        "node_id": node_id,
                        "contract_version": contract_version,
                        "state": "invalidated",
                        "reason": str(row["invalidation_reason"] or ""),
                        "applied": False,
                    }
                    self._log(
                        "test_contract_invalidate",
                        "reused",
                        node_id=node_id,
                        duration_ms=self._elapsed_ms(started),
                        detail=result,
                    )
                    return result
                connection.execute(
                    "UPDATE test_contracts SET state = 'invalidated', invalidation_reason = ?, "
                    "invalidated_at = CURRENT_TIMESTAMP "
                    "WHERE node_id = ? AND contract_version = ? AND state = 'frozen'",
                    (normalized_reason, node_id, contract_version),
                )
                self._record_change(
                    connection,
                    "test_contract",
                    f"{node_id}:{contract_version}",
                    "invalidate",
                    {
                        "node_id": node_id,
                        "contract_version": contract_version,
                        "reason": normalized_reason,
                    },
                )
            result = {
                "node_id": node_id,
                "contract_version": contract_version,
                "state": "invalidated",
                "reason": normalized_reason,
                "applied": True,
            }
            self._log(
                "test_contract_invalidate",
                "completed",
                node_id=node_id,
                duration_ms=self._elapsed_ms(started),
                detail=result,
            )
            return result
        except (ConflictError, NotFoundError, ValidationError) as exc:
            self._log(
                "test_contract_invalidate",
                "rejected",
                node_id=node_id,
                duration_ms=self._elapsed_ms(started),
                detail={
                    "contract_version": contract_version,
                    "error_code": exc.api_error.code,
                },
            )
            raise

    @staticmethod
    def _test_contract_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "node_id": str(row["node_id"]),
            "contract_version": str(row["contract_version"]),
            "snapshot": json.loads(str(row["snapshot"])),
            "state": str(row["state"]),
            "invalidation_reason": (
                None if row["invalidation_reason"] is None else str(row["invalidation_reason"])
            ),
            "frozen_at": str(row["frozen_at"]),
            "invalidated_at": (
                None if row["invalidated_at"] is None else str(row["invalidated_at"])
            ),
        }

    def get_modification_workflow(self, node_id: str) -> dict[str, Any] | None:
        """Read one persisted modification gate without advancing it."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT node_id, state, revision, test_contract_version, updated_at "
                "FROM modification_workflows WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "node_id": str(row["node_id"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "test_contract_version": (
                None
                if row["test_contract_version"] is None
                else str(row["test_contract_version"])
            ),
            "updated_at": str(row["updated_at"]),
        }

    def list_modification_events(self, node_id: str) -> list[dict[str, Any]]:
        """Return the durable audit trail for one modification workflow."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, event_id, node_id, event, from_state, to_state, "
                "revision, payload, created_at FROM modification_events "
                "WHERE node_id = ? ORDER BY sequence",
                (node_id,),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "event_id": str(row["event_id"]),
                "node_id": str(row["node_id"]),
                "event": str(row["event"]),
                "from_state": None if row["from_state"] is None else str(row["from_state"]),
                "to_state": str(row["to_state"]),
                "revision": int(row["revision"]),
                "payload": json.loads(str(row["payload"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def list_modification_workflows(self, *, states: set[str]) -> list[dict[str, Any]]:
        """List persisted workflows in recoverable states without mutating them."""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT node_id, state, revision, test_contract_version, updated_at "
                f"FROM modification_workflows WHERE state IN ({placeholders}) "
                "ORDER BY updated_at, node_id",
                tuple(sorted(states)),
            ).fetchall()
        return [
            {
                "node_id": str(row["node_id"]),
                "state": str(row["state"]),
                "revision": int(row["revision"]),
                "test_contract_version": (
                    None
                    if row["test_contract_version"] is None
                    else str(row["test_contract_version"])
                ),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def enqueue_verification_run(
        self,
        *,
        run_id: str,
        node_id: str,
        phase: str,
        source_revision: int,
        contract_version: str,
        candidate_id: str | None = None,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Persist one idempotent authoritative verification intent."""
        started = time.perf_counter()
        if phase not in {"red", "final"}:
            raise ValidationError(
                resource="verification_run",
                message="Verification phase is invalid",
                details={"phase": phase},
            )
        if max_attempts < 1:
            raise ValidationError(
                resource="verification_run",
                message="Verification retry limit is invalid",
                details={"max_attempts": max_attempts},
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM verification_runs "
                "WHERE node_id = ? AND phase = ? AND source_revision = ? "
                "AND contract_version = ?",
                (node_id, phase, source_revision, contract_version),
            ).fetchone()
            if existing is not None:
                result = self._verification_run_from_row(existing)
                if (
                    result["run_id"] != run_id
                    or result["contract_version"] != contract_version
                    or result["candidate_id"] != candidate_id
                    or result["max_attempts"] != max_attempts
                ):
                    raise ConflictError(
                        resource="verification_run",
                        message="Verification source event is already bound to another run",
                        error_code="verification_run_conflict",
                        details={
                            "node_id": node_id,
                            "phase": phase,
                            "source_revision": source_revision,
                        },
                    )
                self._log(
                    "verification_run_enqueue",
                    "reused",
                    node_id=node_id,
                    duration_ms=self._elapsed_ms(started),
                    detail=self._verification_log_detail(result),
                )
                return result
            connection.execute(
                "INSERT INTO verification_runs"
                "(run_id, node_id, phase, source_revision, contract_version, "
                "candidate_id, state, max_attempts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    run_id,
                    node_id,
                    phase,
                    source_revision,
                    contract_version,
                    candidate_id,
                    max_attempts,
                ),
            )
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        result = self._verification_run_from_row(row)
        self._log(
            "verification_run_enqueue",
            "completed",
            node_id=node_id,
            duration_ms=self._elapsed_ms(started),
            detail=self._verification_log_detail(result),
        )
        return result

    def get_verification_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else self._verification_run_from_row(row)

    def latest_verification_run(self, node_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE node_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (node_id,),
            ).fetchone()
        return None if row is None else self._verification_run_from_row(row)

    def claim_verification_run(
        self,
        run_id: str,
        *,
        now_timestamp: float,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        """Claim a queued or lease-expired verification run exactly once per lease."""
        started = time.perf_counter()
        lease_token = f"verification-lease-{uuid.uuid4()}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    resource="verification_run",
                    message="Verification run not found",
                    details={"run_id": run_id},
                )
            current = self._verification_run_from_row(row)
            if current["state"] in {"completed", "failed"} or (
                current["state"] == "running"
                and current["lease_expires_at"] is not None
                and float(current["lease_expires_at"]) > now_timestamp
            ) or (
                current["state"] == "queued"
                and current["next_retry_at"] is not None
                and float(current["next_retry_at"]) > now_timestamp
            ):
                return None
            connection.execute(
                "UPDATE verification_runs SET state = 'running', attempt = attempt + 1, "
                "lease_token = ?, lease_expires_at = ?, error_code = NULL, "
                "next_retry_at = NULL, terminal_reason = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                (lease_token, now_timestamp + lease_seconds, run_id),
            )
            claimed = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert claimed is not None
        result = self._verification_run_from_row(claimed)
        self._log(
            "verification_run_claim",
            "completed",
            node_id=result["node_id"],
            duration_ms=self._elapsed_ms(started),
            detail=self._verification_log_detail(result),
        )
        return result

    def defer_verification_run(
        self,
        run_id: str,
        *,
        lease_token: str,
        error_code: str,
        now_timestamp: float | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        """Return a temporarily unavailable authoritative run to the durable queue."""
        now = time.time() if now_timestamp is None else now_timestamp
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    resource="verification_run",
                    message="Verification run not found",
                    details={"run_id": run_id},
                )
            current = self._verification_run_from_row(row)
            retry_limit = current["max_attempts"] if max_attempts is None else max_attempts
            if retry_limit < 1:
                raise ValidationError(
                    resource="verification_run",
                    message="Verification retry limit is invalid",
                    details={"max_attempts": retry_limit},
                )
            exhausted = current["attempt"] >= retry_limit
            next_retry_at = (
                None
                if exhausted
                else now + min(float(2 ** max(current["attempt"] - 1, 0)), 60.0)
            )
            updated = connection.execute(
                "UPDATE verification_runs SET state = ?, lease_token = NULL, "
                "lease_expires_at = NULL, next_retry_at = ?, max_attempts = ?, "
                "terminal_reason = ?, error_code = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ? AND state = 'running' AND lease_token = ?",
                (
                    "failed" if exhausted else "queued",
                    next_retry_at,
                    retry_limit,
                    "verification_retry_exhausted" if exhausted else None,
                    error_code,
                    run_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError(
                    resource="verification_run",
                    message="Verification lease is no longer owned",
                    error_code="verification_lease_lost",
                    details={"run_id": run_id},
                )
            if exhausted:
                self._block_verification_workflow_after_retry_exhaustion(
                    connection,
                    run=current,
                    error_code=error_code,
                )
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        result = self._verification_run_from_row(row)
        self._log(
            (
                "verification_run_retry_exhausted"
                if result["state"] == "failed"
                else "verification_run_defer"
            ),
            "blocked" if result["state"] == "failed" else "retry",
            node_id=result["node_id"],
            detail=self._verification_log_detail(result),
        )
        return result

    def renew_verification_lease(
        self,
        run_id: str,
        *,
        lease_token: str,
        now_timestamp: float,
        lease_seconds: int,
    ) -> dict[str, Any]:
        """Extend a still-owned, unexpired verification lease atomically."""
        if lease_seconds < 1:
            raise ValidationError(
                resource="verification_run",
                message="Verification lease duration is invalid",
                details={"lease_seconds": lease_seconds},
            )
        started = time.perf_counter()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE verification_runs SET lease_expires_at = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE run_id = ? "
                "AND state = 'running' AND lease_token = ? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (
                    now_timestamp + lease_seconds,
                    run_id,
                    lease_token,
                    now_timestamp,
                ),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT run_id FROM verification_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(
                        resource="verification_run",
                        message="Verification run not found",
                        details={"run_id": run_id},
                    )
                raise ConflictError(
                    resource="verification_run",
                    message="Verification lease is no longer owned",
                    error_code="verification_lease_lost",
                    details={"run_id": run_id},
                )
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        result = self._verification_run_from_row(row)
        self._log(
            "verification_run_lease_renew",
            "completed",
            node_id=result["node_id"],
            duration_ms=self._elapsed_ms(started),
            detail=self._verification_log_detail(result),
        )
        return result

    def _block_verification_workflow_after_retry_exhaustion(
        self,
        connection: sqlite3.Connection,
        *,
        run: dict[str, Any],
        error_code: str,
    ) -> None:
        from bridle.agent.runtime.modification_workflow import (
            ModificationEvent,
            ModificationState,
        )

        node_id = str(run["node_id"])
        phase = str(run["phase"])
        expected_state = {
            "red": ModificationState.RED_VERIFYING.value,
            "final": ModificationState.FINAL_VERIFYING.value,
        }[phase]
        workflow = connection.execute(
            "SELECT state, revision, test_contract_version "
            "FROM modification_workflows WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if workflow is None or str(workflow["state"]) != expected_state:
            raise ConflictError(
                resource="modification_workflow",
                message="Verification workflow is no longer active",
                error_code="verification_workflow_state_conflict",
                details={
                    "node_id": node_id,
                    "phase": phase,
                    "expected_state": expected_state,
                    "actual_state": None if workflow is None else str(workflow["state"]),
                },
            )
        current_revision = int(workflow["revision"])
        revision = current_revision + 1
        target_state = ModificationState.VERIFICATION_BLOCKED.value
        updated = connection.execute(
            "UPDATE modification_workflows SET state = ?, revision = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE node_id = ? AND revision = ?",
            (target_state, revision, node_id, current_revision),
        )
        if updated.rowcount != 1:
            raise ConflictError(
                resource="modification_workflow",
                message="Modification workflow changed concurrently",
                error_code="modification_revision_conflict",
                details={"node_id": node_id, "actual_revision": current_revision},
            )
        event = ModificationEvent.VERIFICATION_RETRY_EXHAUSTED.value
        event_id = f"verification:{run['run_id']}:retry-exhausted"
        payload = {
            "verification_run_id": str(run["run_id"]),
            "phase": phase,
            "status": "blocked",
            "error_code": error_code,
            "terminal_reason": "verification_retry_exhausted",
        }
        contract_version = workflow["test_contract_version"]
        if contract_version is not None:
            payload["test_contract_version"] = str(contract_version)
        connection.execute(
            "INSERT INTO modification_events"
            "(event_id, node_id, event, from_state, to_state, revision, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                node_id,
                event,
                expected_state,
                target_state,
                revision,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        self._record_change(
            connection,
            "modification_workflow",
            node_id,
            "transition",
            {
                "event": event,
                "event_id": event_id,
                "from_state": expected_state,
                "to_state": target_state,
                "revision": revision,
                "test_contract_version": (
                    None if contract_version is None else str(contract_version)
                ),
            },
        )

    def next_verification_retry_at(self) -> float | None:
        """Return the nearest durable retry deadline for background scheduling."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(next_retry_at) FROM verification_runs "
                "WHERE state = 'queued' AND next_retry_at IS NOT NULL"
            ).fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def complete_verification_run(
        self,
        run_id: str,
        *,
        lease_token: str,
        outcome: dict[str, Any],
        error_code: str | None,
    ) -> dict[str, Any]:
        """Persist the only authoritative result accepted for one source event."""
        safe_outcome = json.dumps(
            outcome,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE verification_runs SET state = 'completed', outcome = ?, "
                "error_code = ?, lease_token = NULL, lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ? AND state = 'running' AND lease_token = ?",
                (safe_outcome, error_code, run_id, lease_token),
            )
            if updated.rowcount != 1:
                current = connection.execute(
                    "SELECT * FROM verification_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if current is not None and str(current["state"]) == "completed":
                    return self._verification_run_from_row(current)
                raise ConflictError(
                    resource="verification_run",
                    message="Verification lease is no longer owned",
                    error_code="verification_lease_lost",
                    details={"run_id": run_id},
                )
            row = connection.execute(
                "SELECT * FROM verification_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        result = self._verification_run_from_row(row)
        self._log(
            "verification_run_complete",
            "completed",
            node_id=result["node_id"],
            detail=self._verification_log_detail(result),
        )
        return result

    @staticmethod
    def _verification_run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "run_id": str(row["run_id"]),
            "node_id": str(row["node_id"]),
            "phase": str(row["phase"]),
            "source_revision": int(row["source_revision"]),
            "contract_version": str(row["contract_version"]),
            "candidate_id": (
                None if row["candidate_id"] is None else str(row["candidate_id"])
            ),
            "state": str(row["state"]),
            "attempt": int(row["attempt"]),
            "next_retry_at": (
                None if row["next_retry_at"] is None else float(row["next_retry_at"])
            ),
            "max_attempts": int(row["max_attempts"]),
            "terminal_reason": (
                None if row["terminal_reason"] is None else str(row["terminal_reason"])
            ),
            "lease_token": None if row["lease_token"] is None else str(row["lease_token"]),
            "lease_expires_at": (
                None if row["lease_expires_at"] is None else float(row["lease_expires_at"])
            ),
            "outcome": None if row["outcome"] is None else json.loads(str(row["outcome"])),
            "error_code": None if row["error_code"] is None else str(row["error_code"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "completed_at": (
                None if row["completed_at"] is None else str(row["completed_at"])
            ),
        }

    @staticmethod
    def _verification_log_detail(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run["run_id"],
            "phase": run["phase"],
            "source_revision": run["source_revision"],
            "contract_version": run["contract_version"],
            "candidate_id": run["candidate_id"],
            "state": run["state"],
            "attempt": run["attempt"],
            "next_retry_at": run["next_retry_at"],
            "max_attempts": run["max_attempts"],
            "terminal_reason": run["terminal_reason"],
            "error_code": run["error_code"],
        }

    def apply_modification_transition(
        self,
        node_id: str,
        *,
        event: str,
        event_id: str,
        allowed_from: set[str | None],
        to_state: str,
        expected_revision: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically append one idempotent modification transition."""
        started = time.perf_counter()
        current_state: str | None = None
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing_event = connection.execute(
                    "SELECT node_id, event, to_state, revision, payload "
                    "FROM modification_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing_event is not None:
                    if (
                        str(existing_event["node_id"]) != node_id
                        or str(existing_event["event"]) != event
                        or str(existing_event["to_state"]) != to_state
                    ):
                        raise ConflictError(
                            resource="modification_workflow",
                            message="Modification event ID is already bound to another transition",
                            error_code="modification_event_conflict",
                            details={"event_id": event_id, "node_id": node_id},
                        )
                    existing_payload = json.loads(str(existing_event["payload"]))
                    result = {
                        "node_id": node_id,
                        "state": str(existing_event["to_state"]),
                        "revision": int(existing_event["revision"]),
                        "test_contract_version": existing_payload.get(
                            "test_contract_version"
                        ),
                        "applied": False,
                    }
                    self._log(
                        "modification_transition",
                        "reused",
                        node_id=node_id,
                        duration_ms=self._elapsed_ms(started),
                        detail={"event": event, "event_id": event_id, **result},
                    )
                    return result

                current = connection.execute(
                    "SELECT state, revision, test_contract_version "
                    "FROM modification_workflows WHERE node_id = ?",
                    (node_id,),
                ).fetchone()
                current_state = None if current is None else str(current["state"])
                current_revision = 0 if current is None else int(current["revision"])
                current_contract_version = (
                    None
                    if current is None or current["test_contract_version"] is None
                    else str(current["test_contract_version"])
                )
                if expected_revision is not None and expected_revision != current_revision:
                    raise ConflictError(
                        resource="modification_workflow",
                        message="Modification workflow revision changed",
                        error_code="modification_revision_conflict",
                        details={
                            "node_id": node_id,
                            "expected_revision": expected_revision,
                            "actual_revision": current_revision,
                        },
                    )
                if current_state not in allowed_from:
                    raise ConflictError(
                        resource="modification_workflow",
                        message="Modification transition is not allowed from the current state",
                        error_code="modification_transition_invalid",
                        details={
                            "node_id": node_id,
                            "event": event,
                            "from_state": current_state,
                            "allowed_from": sorted(value for value in allowed_from if value is not None),
                        },
                    )

                safe_payload = dict(payload or {})
                next_contract_version = current_contract_version
                if event in {"test_contract_approved", "red_allowed"}:
                    active_contract = connection.execute(
                        "SELECT contract_version FROM test_contracts "
                        "WHERE node_id = ? AND state = 'frozen'",
                        (node_id,),
                    ).fetchone()
                    if active_contract is None:
                        raise ConflictError(
                            resource="test_contract",
                            message="A frozen test contract is required for this transition",
                            error_code="test_contract_required",
                            details={"node_id": node_id, "event": event},
                        )
                    active_contract_version = str(active_contract["contract_version"])
                    if event == "test_contract_approved":
                        next_contract_version = active_contract_version
                    elif current_contract_version != active_contract_version:
                        raise ConflictError(
                            resource="test_contract",
                            message="The approved frozen test contract is no longer current",
                            error_code="test_contract_stale",
                            details={
                                "node_id": node_id,
                                "approved_contract_version": current_contract_version,
                                "active_contract_version": active_contract_version,
                            },
                        )
                    safe_payload["test_contract_version"] = active_contract_version
                elif event in {"invalid_test", "baseline_expired"}:
                    if current_contract_version is not None:
                        connection.execute(
                            "UPDATE test_contracts SET state = 'invalidated', "
                            "invalidation_reason = ?, invalidated_at = CURRENT_TIMESTAMP "
                            "WHERE node_id = ? AND contract_version = ? AND state = 'frozen'",
                            (event, node_id, current_contract_version),
                        )
                        safe_payload["invalidated_contract_version"] = current_contract_version
                    next_contract_version = None
                elif next_contract_version is not None:
                    safe_payload["test_contract_version"] = next_contract_version

                revision = current_revision + 1
                if current is None:
                    connection.execute(
                        "INSERT INTO modification_workflows"
                        "(node_id, state, revision, test_contract_version) VALUES (?, ?, ?, ?)",
                        (node_id, to_state, revision, next_contract_version),
                    )
                else:
                    updated = connection.execute(
                        "UPDATE modification_workflows SET state = ?, revision = ?, "
                        "test_contract_version = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE node_id = ? AND revision = ?",
                        (
                            to_state,
                            revision,
                            next_contract_version,
                            node_id,
                            current_revision,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ConflictError(
                            resource="modification_workflow",
                            message="Modification workflow changed concurrently",
                            error_code="modification_revision_conflict",
                            details={"node_id": node_id, "actual_revision": current_revision},
                        )
                connection.execute(
                    "INSERT INTO modification_events"
                    "(event_id, node_id, event, from_state, to_state, revision, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        node_id,
                        event,
                        current_state,
                        to_state,
                        revision,
                        json.dumps(
                            safe_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                self._record_change(
                    connection,
                    "modification_workflow",
                    node_id,
                    "transition",
                    {
                        "event": event,
                        "event_id": event_id,
                        "from_state": current_state,
                        "to_state": to_state,
                        "revision": revision,
                        "test_contract_version": next_contract_version,
                    },
                )
            result = {
                "node_id": node_id,
                "state": to_state,
                "revision": revision,
                "test_contract_version": next_contract_version,
                "applied": True,
            }
            self._log(
                "modification_transition",
                "completed",
                node_id=node_id,
                duration_ms=self._elapsed_ms(started),
                detail={
                    "event": event,
                    "event_id": event_id,
                    "from_state": current_state,
                    **result,
                },
            )
            return result
        except ConflictError as exc:
            self._log(
                "modification_transition",
                "rejected",
                node_id=node_id,
                duration_ms=self._elapsed_ms(started),
                detail={
                    "event": event,
                    "event_id": event_id,
                    "from_state": current_state,
                    "to_state": to_state,
                    "error_code": exc.api_error.code,
                },
            )
            raise

    def dispatch_child_agent(self, node_id: str, *, target_role: str) -> dict[str, Any]:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT message_id FROM child_spawn_facts WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            spawn_message_id = (
                str(existing[0]) if existing is not None else f"spawn-{uuid.uuid4()}"
            )
            result = ModifyLoopService.dispatch_child_agent(
                connection, node_id=node_id, target_role=target_role
            )
            connection.execute(
                "INSERT OR IGNORE INTO child_spawn_facts(message_id, node_id, target_role) "
                "VALUES (?, ?, ?)",
                (spawn_message_id, node_id, target_role),
            )
            self._record_change(connection, "plan_node", node_id, "dispatch", {"target_role": target_role})
        return {**result, "spawn_message_id": spawn_message_id}

    def apply_child_result(
        self,
        *,
        message_id: str,
        node_id: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one terminal child result and receipt in the same plan transaction."""
        if status not in {"completed", "failed", "cancelled"}:
            raise ValidationError(
                resource="child_result",
                message="Unsupported child result status",
                details={"status": status},
            )
        plan_status = "completed" if status == "completed" else "failed"
        result_payload = result or {}
        result_json = json.dumps(
            result_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT node_id, result_status, result_json "
                "FROM child_result_receipts WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "node_id": str(existing["node_id"]),
                    "status": str(existing["result_status"]),
                    "applied": False,
                }
            spawn = connection.execute(
                "SELECT message_id FROM child_spawn_facts WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            if spawn is None or message_id != f"child-result-{spawn['message_id']}":
                raise ValidationError(
                    resource="child_result",
                    message="Child result does not match a dispatched plan node",
                    details={"message_id": message_id, "node_id": node_id},
                )
            connection.execute(
                "UPDATE plan_nodes SET status = ? WHERE id = ? AND archived = 0",
                (plan_status, node_id),
            )
            connection.execute(
                "UPDATE child_spawn_facts SET status = ? WHERE node_id = ?",
                (status, node_id),
            )
            connection.execute(
                "INSERT INTO child_result_receipts"
                "(message_id, node_id, result_status, result_json) "
                "VALUES (?, ?, ?, ?)",
                (message_id, node_id, status, result_json),
            )
            self._record_change(
                connection,
                "plan_node",
                node_id,
                "child_result",
                {"message_id": message_id, "result_status": status},
            )
        return {"node_id": node_id, "status": status, "applied": True}

    def verify_node(
        self,
        node_id: str,
        *,
        exposed_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        """Complete a node only from persisted READY_TO_PUBLISH evidence."""
        try:
            with self._connect() as connection:
                ModifyLoopService.check_consistency_gate(
                    connection,
                    node_id=node_id,
                    exposed_symbols=exposed_symbols or set(),
                )
                modification = connection.execute(
                    "SELECT state, revision FROM modification_workflows WHERE node_id = ?",
                    (node_id,),
                ).fetchone()
                modification_state = None if modification is None else str(modification["state"])
                if modification_state not in {"READY_TO_PUBLISH", "PUBLISHED"}:
                    raise ConflictError(
                        resource="modification_workflow",
                        message="Node verification requires persisted READY_TO_PUBLISH evidence",
                        error_code=TDD_GATE_ERROR,
                        details={
                            "node_id": node_id,
                            "modification_state": modification_state,
                            "revision": None if modification is None else int(modification["revision"]),
                        },
                    )
                connection.execute(
                    "UPDATE plan_nodes SET status = 'completed' WHERE id = ?", (node_id,)
                )
                self._record_change(connection, "plan_node", node_id, "verify_passed", {})
        except ConflictError as exc:
            with self._connect() as connection:
                operation = "verify_blocked" if exc.api_error.code == TDD_GATE_ERROR else "verify_failed"
                if operation == "verify_failed":
                    connection.execute(
                        "UPDATE plan_nodes SET status = 'failed' WHERE id = ?", (node_id,)
                    )
                self._record_change(
                    connection,
                    "plan_node",
                    node_id,
                    operation,
                    {"error": exc.api_error.code},
                )
            raise
        return self.get_node(node_id)

    def declare_module_interface(
        self,
        *,
        from_module: str,
        to_module: str,
        symbol: str,
        signature: dict[str, Any],
        mock: dict[str, Any],
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            return ModifyLoopService.declare_interface(
                connection,
                from_module=from_module,
                to_module=to_module,
                symbol=symbol,
                signature=signature,
                mock=mock,
                confidence=confidence,
            )

    def mock_readonly_paths_for_node(self, node_id: str) -> list[str]:
        with self._connect() as connection:
            return ModifyLoopService.mock_readonly_paths(connection, node_id=node_id)

    def latest_change_seq(self) -> int:
        return self._latest_change_seq()

    def module_interfaces_for_node(self, node_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, from_module, to_module, symbol, signature, mock, confidence, status "
                "FROM module_interfaces WHERE from_module = ? OR to_module = ?",
                (node_id, node_id),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                for key in ("signature", "mock"):
                    try:
                        item[key] = json.loads(item[key]) if item.get(key) else {}
                    except (TypeError, json.JSONDecodeError):
                        item[key] = {}
                result.append(item)
            return result

    def module_interfaces_for_module(self, module_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, from_module, to_module, symbol, signature, mock, confidence, status "
                "FROM module_interfaces WHERE from_module = ? OR to_module = ?",
                (module_id, module_id),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                for key in ("signature", "mock"):
                    try:
                        item[key] = json.loads(item[key]) if item.get(key) else {}
                    except (TypeError, json.JSONDecodeError):
                        item[key] = {}
                result.append(item)
            return result

    def _refresh_semantic_map_candidates(self, connection: sqlite3.Connection, *, reason: str) -> dict[str, Any]:
        """Regenerate deterministic module/interface candidates from current structure rows."""
        run_id = f"semmap-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO semantic_map_runs(id, status, reason, payload) VALUES (?, 'running', ?, '{}')",
            (run_id, reason),
        )
        previous_modules = {
            (str(row["id"]), str(row["file_fingerprint"])): {
                "status": str(row["status"]),
                "confirmed_at": row["confirmed_at"],
            }
            for row in connection.execute(
                "SELECT id, file_fingerprint, status, confirmed_at FROM module_candidates"
            ).fetchall()
        }
        previous_interfaces = {
            self._interface_candidate_status_key(dict(row)): {
                "status": str(row["status"]),
                "confirmed_at": row["confirmed_at"],
            }
            for row in connection.execute(
                "SELECT id, from_module, to_module, from_candidate_id, to_candidate_id, "
                "symbol, signature, mock_hash, status, confirmed_at FROM module_interface_candidates"
            ).fetchall()
        }
        generated = self._synthesis.synthesize(connection, run_id=run_id)
        now = "CURRENT_TIMESTAMP"

        connection.execute(
            "UPDATE module_candidates SET status = 'stale' WHERE status IN ('candidate', 'confirmed')"
        )
        connection.execute(
            "UPDATE module_interface_candidates SET status = 'stale' WHERE status IN ('candidate', 'confirmed')"
        )
        self._revoke_stale_candidate_interfaces(connection)
        connection.execute(
            "UPDATE interface_mock_artifacts SET status = 'stale' WHERE status IN ('generated', 'confirmed')"
        )
        connection.execute("DELETE FROM module_edges")

        evidence = generated["evidence"]
        connection.execute(
            "INSERT OR REPLACE INTO semantic_evidence_bundles(id, run_id, kind, payload) VALUES (?, ?, ?, ?)",
            (
                evidence["id"],
                evidence["run_id"],
                evidence["kind"],
                json.dumps(evidence["payload"], ensure_ascii=False),
            ),
        )

        for candidate in generated["module_candidates"]:
            prior = previous_modules.get((candidate["id"], candidate["file_fingerprint"]), {})
            status = prior.get("status") if prior.get("status") in ("confirmed", "rejected") else candidate["status"]
            confirmed_at = prior.get("confirmed_at") if status == "confirmed" else None
            connection.execute(
                "INSERT OR REPLACE INTO module_candidates("
                "id, run_id, module_id, name, status, confidence, evidence_id, metrics, "
                "file_fingerprint, created_at, confirmed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate["id"],
                    candidate["run_id"],
                    candidate["module_id"],
                    candidate["name"],
                    status,
                    float(candidate["confidence"]),
                    candidate["evidence_id"],
                    json.dumps(candidate["metrics"], ensure_ascii=False),
                    candidate["file_fingerprint"],
                    candidate["created_at"],
                    confirmed_at,
                ),
            )
            connection.execute("DELETE FROM module_candidate_files WHERE candidate_id = ?", (candidate["id"],))

        for item in generated["module_candidate_files"]:
            connection.execute(
                "INSERT OR REPLACE INTO module_candidate_files("
                "candidate_id, file_path, role, file_hash, evidence"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    item["candidate_id"],
                    item["file_path"],
                    item["role"],
                    item["file_hash"],
                    json.dumps(item["evidence"], ensure_ascii=False),
                ),
            )

        for edge in generated["module_edges"]:
            connection.execute(
                "INSERT OR REPLACE INTO module_edges("
                "id, run_id, source_candidate_id, target_candidate_id, source_module, "
                "target_module, kind, weight, evidence"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    edge["id"],
                    edge["run_id"],
                    edge["source_candidate_id"],
                    edge["target_candidate_id"],
                    edge["source_module"],
                    edge["target_module"],
                    edge["kind"],
                    float(edge["weight"]),
                    json.dumps(edge["evidence"], ensure_ascii=False),
                ),
            )

        for candidate in generated["module_interface_candidates"]:
            prior = previous_interfaces.get(self._interface_candidate_status_key(candidate), {})
            status = prior.get("status") if prior.get("status") in ("confirmed", "rejected") else candidate["status"]
            confirmed_at = prior.get("confirmed_at") if status == "confirmed" else None
            connection.execute(
                "INSERT OR REPLACE INTO module_interface_candidates("
                "id, run_id, from_module, to_module, from_candidate_id, to_candidate_id, symbol, "
                "signature, evidence, mock_file_path, mock_hash, confidence, status, created_at, confirmed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate["id"],
                    candidate["run_id"],
                    candidate["from_module"],
                    candidate["to_module"],
                    candidate["from_candidate_id"],
                    candidate["to_candidate_id"],
                    candidate["symbol"],
                    json.dumps(candidate["signature"], ensure_ascii=False),
                    json.dumps(candidate["evidence"], ensure_ascii=False),
                    candidate["mock_file_path"],
                    candidate["mock_hash"],
                    float(candidate["confidence"]),
                    status,
                    candidate["created_at"],
                    confirmed_at,
                ),
            )
            if status == "confirmed":
                self._publish_confirmed_interface(connection, candidate["id"])

        for artifact in generated["interface_mock_artifacts"]:
            candidate_status = connection.execute(
                "SELECT status FROM module_interface_candidates WHERE id = ?",
                (artifact["interface_candidate_id"],),
            ).fetchone()["status"]
            status = {
                "confirmed": "confirmed",
                "rejected": "rejected",
            }.get(str(candidate_status), artifact["status"])
            connection.execute(
                "INSERT OR REPLACE INTO interface_mock_artifacts("
                "id, interface_candidate_id, file_path, file_hash, status, payload, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact["id"],
                    artifact["interface_candidate_id"],
                    artifact["file_path"],
                    artifact["file_hash"],
                    status,
                    json.dumps(artifact["payload"], ensure_ascii=False),
                    artifact["created_at"],
                ),
            )

        module_count = len(generated["module_candidates"])
        interface_count = len(generated["module_interface_candidates"])
        connection.execute(
            f"UPDATE semantic_map_runs SET status = 'ready', completed_at = {now}, payload = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "module_candidates": module_count,
                        "module_interface_candidates": interface_count,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        self._record_change(
            connection,
            "semantic_map_run",
            run_id,
            "ready",
            {"module_candidates": module_count, "module_interface_candidates": interface_count, "reason": reason},
        )
        self._log(
            "semantic_map_candidates",
            "completed",
            detail={"run_id": run_id, "module_candidates": module_count, "interface_candidates": interface_count},
        )
        return {
            "run_id": run_id,
            "status": "ready",
            "module_candidates": module_count,
            "module_interface_candidates": interface_count,
        }

    @staticmethod
    def _module_candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "module_id": row["module_id"],
            "name": row["name"],
            "status": row["status"],
            "confidence": row["confidence"],
            "evidence_id": row["evidence_id"],
            "metrics": json.loads(row["metrics"] or "{}"),
            "file_fingerprint": row["file_fingerprint"],
            "is_execution_boundary": row["status"] == "confirmed",
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
        }

    @staticmethod
    def _interface_candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "from_module": row["from_module"],
            "to_module": row["to_module"],
            "from_candidate_id": row["from_candidate_id"],
            "to_candidate_id": row["to_candidate_id"],
            "symbol": row["symbol"],
            "signature": json.loads(row["signature"] or "{}"),
            "evidence": json.loads(row["evidence"] or "{}"),
            "mock_file_path": row["mock_file_path"],
            "mock_hash": row["mock_hash"],
            "confidence": row["confidence"],
            "status": row["status"],
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
        }

    @staticmethod
    def _mock_artifact_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "interface_candidate_id": row["interface_candidate_id"],
            "file_path": row["file_path"],
            "file_hash": row["file_hash"],
            "status": row["status"],
            "payload": json.loads(row["payload"] or "{}"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _module_candidate_files(connection: sqlite3.Connection, candidate_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT file_path, role, file_hash, evidence FROM module_candidate_files "
            "WHERE candidate_id = ? ORDER BY file_path",
            (candidate_id,),
        ).fetchall()
        return [
            {
                "file_path": row["file_path"],
                "role": row["role"],
                "file_hash": row["file_hash"],
                "evidence": json.loads(row["evidence"] or "{}"),
            }
            for row in rows
        ]

    def _confirmed_candidate_boundary(self, candidate_or_module_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM module_candidates WHERE status = 'confirmed' "
                "AND (id = ? OR module_id = ?) ORDER BY confirmed_at DESC, module_id LIMIT 1",
                (candidate_or_module_id, candidate_or_module_id),
            ).fetchone()
            if row is None:
                return None
            item = self._module_candidate_from_row(row)
            item["files"] = self._module_candidate_files(connection, item["id"])
            return item

    @staticmethod
    def _interface_candidate_status_key(item: dict[str, Any]) -> tuple[str, str, str, str, str, str, str, str]:
        signature = item.get("signature")
        if isinstance(signature, str):
            try:
                signature = json.loads(signature) if signature else {}
            except json.JSONDecodeError:
                signature = {}
        return (
            str(item.get("id", "")),
            str(item.get("from_module", "")),
            str(item.get("to_module", "")),
            str(item.get("from_candidate_id", "")),
            str(item.get("to_candidate_id", "")),
            str(item.get("symbol", "")),
            json.dumps(signature if isinstance(signature, dict) else {}, ensure_ascii=False, sort_keys=True),
            str(item.get("mock_hash", "")),
        )

    @staticmethod
    def _revoke_candidate_interface(connection: sqlite3.Connection, candidate_id: str) -> None:
        connection.execute("DELETE FROM module_interfaces WHERE id = ?", (f"iface-{candidate_id}",))

    @staticmethod
    def _revoke_stale_candidate_interfaces(connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM module_interfaces "
            "WHERE id IN (SELECT 'iface-' || id FROM module_interface_candidates WHERE status = 'stale')"
        )

    def _publish_confirmed_interface(self, connection: sqlite3.Connection, candidate_id: str) -> None:
        row = connection.execute(
            "SELECT * FROM module_interface_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return
        item = self._interface_candidate_from_row(row)
        interface_id = f"iface-{candidate_id}"
        mock = {
            "file_path": item["mock_file_path"],
            "mock_hash": item["mock_hash"],
            "interface_candidate_id": candidate_id,
        }
        connection.execute(
            "INSERT OR REPLACE INTO module_interfaces("
            "id, from_module, to_module, symbol, signature, mock, confidence, status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, 'declared')",
            (
                interface_id,
                item["from_module"],
                item["to_module"],
                item["symbol"],
                json.dumps(item["signature"], ensure_ascii=False),
                json.dumps(mock, ensure_ascii=False),
                float(item["confidence"]),
            ),
        )

    @staticmethod
    def _module_search_prefixes(declared_files: list[str]) -> set[str]:
        prefixes: set[str] = set()
        for path in declared_files:
            parts = path.split("/")
            if len(parts) <= 1:
                prefixes.add("")
                continue
            for index in range(1, len(parts)):
                prefixes.add("/".join(parts[:index]))
            prefixes.add("")
        return prefixes or {""}

    def module_execution_snapshot(self, node_id: str) -> dict[str, Any]:
        """Authoritative module boundary for container execution from code map entities."""
        from bridle.features.project_map.indexer.treesitter_indexer import classify_is_test

        node = self.get_node(node_id)
        explicit_candidate_id = str(node.get("module_candidate_id") or "").strip()
        module_id = str(node.get("module_id") or explicit_candidate_id or node_id)
        declared_files = [
            str(path).replace("\\", "/").strip()
            for path in node.get("files") or []
            if str(path).strip()
        ]
        if not declared_files and (node.get("module_id") or explicit_candidate_id):
            candidate = self._confirmed_candidate_boundary(explicit_candidate_id or module_id)
            if candidate is None:
                return {
                    "error_code": "module_boundary_unconfirmed",
                    "detail": {"module_id": module_id, "reason": "confirmed_module_candidate_required"},
                }
            module_id = str(candidate["module_id"])
            declared_files = [
                str(item["file_path"])
                for item in candidate.get("files", [])
                if item.get("role") == "implementation"
            ]
        test_dir_raw = node.get("test_dir")
        test_dir = (
            str(test_dir_raw).replace("\\", "/").strip("/")
            if isinstance(test_dir_raw, str) and test_dir_raw.strip()
            else None
        )
        declared_test_dirs = self._declared_test_dirs()
        module_prefixes = self._module_search_prefixes(declared_files)

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, path, kind, payload FROM code_entities WHERE kind IN ('file', 'test')"
            ).fetchall()

        by_path: dict[str, dict[str, Any]] = {}
        for row in rows:
            path = str(row["path"])
            by_path[path] = {
                "entity_id": str(row["id"]),
                "path": path,
                "kind": str(row["kind"]),
                "file_hash": self._file_content_hash(path),
            }

        implementation_entities: list[dict[str, Any]] = []
        for path in declared_files:
            entity = by_path.get(path)
            if entity is None or entity["kind"] != "file":
                return {
                    "error_code": "module_boundary_incomplete",
                    "detail": {"path": path, "reason": "missing_implementation_entity"},
                }
            if not entity["file_hash"]:
                return {
                    "error_code": "module_boundary_incomplete",
                    "detail": {"path": path, "reason": "missing_implementation_file"},
                }
            implementation_entities.append(entity)

        test_entities: list[dict[str, Any]] = []
        for path, entity in sorted(by_path.items()):
            if entity["kind"] != "test":
                continue
            belongs = False
            if test_dir is not None:
                belongs = path == test_dir or path.startswith(f"{test_dir}/")
                if belongs:
                    belongs = any(
                        path.startswith(f"{prefix}/") if prefix else True for prefix in module_prefixes
                    )
            else:
                for prefix in sorted(module_prefixes, key=len, reverse=True):
                    segment = f"{prefix}/tests/" if prefix else "tests/"
                    if path.startswith(segment) and classify_is_test(path, declared_test_dirs):
                        belongs = True
                        break
            if not belongs:
                continue
            if not entity["file_hash"]:
                return {
                    "error_code": "module_boundary_incomplete",
                    "detail": {"path": path, "reason": "missing_test_file"},
                }
            test_entities.append(entity)

        interfaces: list[dict[str, Any]] = []
        for row in self.module_interfaces_for_module(module_id):
            mock_payload = row.get("mock") if isinstance(row.get("mock"), dict) else {}
            file_path = str(mock_payload.get("file_path") or "").replace("\\", "/").strip()
            if not file_path:
                return {
                    "error_code": "module_boundary_incomplete",
                    "detail": {"interface_id": row.get("id"), "reason": "missing_interface_mock"},
                }
            mock_hash = self._file_content_hash(file_path)
            if not mock_hash:
                return {
                    "error_code": "module_boundary_incomplete",
                    "detail": {"path": file_path, "reason": "missing_interface_mock_file"},
                }
            interfaces.append(
                {
                    "interface_id": str(row.get("id", "")),
                    "from_module": str(row.get("from_module", "")),
                    "to_module": str(row.get("to_module", "")),
                    "file_path": file_path,
                    "mock_hash": mock_hash,
                    "entity_version": mock_hash,
                }
            )

        test_commands = [str(item).strip() for item in node.get("tests") or [] if str(item).strip()]
        return {
            "module_id": module_id,
            "node_id": node_id,
            "implementation_entities": implementation_entities,
            "test_entities": test_entities,
            "test_commands": test_commands,
            "test_dir": test_dir,
            "interfaces": interfaces,
        }

    def scip_occurrences_for_file(self, rel_path: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT file_path, moniker, role, range FROM code_occurrences WHERE file_path = ?",
                (rel_path,),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _apply_test_classification(entities: list[dict], test_paths: set[str]) -> list[dict]:
        """Reclassify scanned file entities as test files (kind='test', re-keyed) when under a test dir."""
        result: list[dict] = []
        for entity in entities:
            if entity["kind"] == "file" and entity["path"] in test_paths:
                entity = {
                    **entity,
                    "kind": "test",
                    "id": WorkspaceOverviewService._entity_id("test", entity["path"]),
                }
            result.append(entity)
        return result

    def _declared_test_dirs(self, connection: sqlite3.Connection | None = None) -> set[str]:
        """Read module test_dir declarations from plan_nodes.payload; output is normalized relative dirs."""
        if connection is None:
            with self._connect() as owned:
                return self._declared_test_dirs(owned)
        rows = connection.execute(
            "SELECT payload FROM plan_nodes WHERE archived = 0"
        ).fetchall()
        declared: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            test_dir = payload.get("test_dir") if isinstance(payload, dict) else None
            if isinstance(test_dir, str) and test_dir.strip():
                declared.add(test_dir.replace("\\", "/").strip("/"))
        return declared

    def _workspace_file_entities(
        self,
        connection: sqlite3.Connection,
        normalized: list[str],
        scanned: list[dict],
    ) -> list[dict]:
        """Assemble the full file set for import resolution from the DB plus freshly scanned files."""
        rows = connection.execute(
            "SELECT path, id, kind FROM code_entities WHERE kind IN ('file', 'test')"
        ).fetchall()
        file_map: dict[str, dict] = {
            str(row["path"]): {"path": str(row["path"]), "id": str(row["id"]), "kind": str(row["kind"])}
            for row in rows
        }
        for entity in scanned:
            if entity["kind"] == "file":
                file_map[entity["path"]] = {
                    "path": entity["path"],
                    "id": entity["id"],
                    "kind": "file",
                }
        for rel_path in normalized:
            if not self.project_root.joinpath(*rel_path.split("/")).exists():
                file_map.pop(rel_path, None)
        return list(file_map.values())

    @staticmethod
    def _entity_ids_for_paths(connection: sqlite3.Connection, rel_paths: list[str]) -> list[str]:
        entity_ids: list[str] = []
        for rel_path in rel_paths:
            rows = connection.execute(
                "SELECT id FROM code_entities WHERE path = ? OR path LIKE ? OR path LIKE ?",
                (rel_path, f"{rel_path}/%", f"{rel_path}::%"),
            ).fetchall()
            entity_ids.extend(str(row[0]) for row in rows)
        return entity_ids

    @staticmethod
    def _collect_relation_rebuild_paths(
        connection: sqlite3.Connection,
        stale_ids: list[str],
        normalized: set[str],
    ) -> set[str]:
        if not stale_ids:
            return set()
        placeholders = ",".join("?" for _ in stale_ids)
        rows = connection.execute(
            f"SELECT source_id, target_id FROM code_relations "
            f"WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            (*stale_ids, *stale_ids),
        ).fetchall()
        stale_set = set(stale_ids)
        rebuild: set[str] = set()
        for row in rows:
            for entity_id in (str(row[0]), str(row[1])):
                if entity_id in stale_set:
                    continue
                path_row = connection.execute(
                    "SELECT path FROM code_entities WHERE id = ?", (entity_id,)
                ).fetchone()
                if path_row is None:
                    continue
                file_path = str(path_row[0]).split("::", 1)[0]
                if file_path not in normalized:
                    rebuild.add(file_path)
        return rebuild

    @staticmethod
    def _purge_path_artifacts(connection: sqlite3.Connection, rel_path: str) -> None:
        stale_ids = ProjectPlanStore._entity_ids_for_paths(connection, [rel_path])
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            connection.execute(
                f"DELETE FROM code_relations WHERE source_id IN ({placeholders}) "
                f"OR target_id IN ({placeholders})",
                (*stale_ids, *stale_ids),
            )
        connection.execute(
            "DELETE FROM code_entities WHERE path = ? OR path LIKE ? OR path LIKE ?",
            (rel_path, f"{rel_path}/%", f"{rel_path}::%"),
        )
        connection.execute("DELETE FROM code_occurrences WHERE file_path = ?", (rel_path,))
        connection.execute("DELETE FROM code_symbols WHERE moniker GLOB ?", (f"{rel_path}::*",))
        connection.execute("DELETE FROM map_blind_spots WHERE file_path = ?", (rel_path,))

    @staticmethod
    def _delete_outgoing_non_contains_relations_for_paths(
        connection: sqlite3.Connection,
        rel_paths: list[str],
    ) -> None:
        """Remove only edges emitted by rebuild paths; preserve incoming edges from callers."""
        entity_ids = ProjectPlanStore._entity_ids_for_paths(connection, rel_paths)
        if not entity_ids:
            return
        placeholders = ",".join("?" for _ in entity_ids)
        connection.execute(
            f"DELETE FROM code_relations WHERE kind != 'contains' AND source_id IN ({placeholders})",
            entity_ids,
        )

    def _insert_code_entities(
        self,
        connection: sqlite3.Connection,
        entities: list[dict],
        symbol_entities: list[dict],
        relations: list[dict],
    ) -> None:
        for entity in [*entities, *symbol_entities]:
            connection.execute(
                "INSERT INTO code_entities(id, path, kind, name, parent_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET path = excluded.path, kind = excluded.kind, "
                "name = excluded.name, parent_id = excluded.parent_id, payload = excluded.payload",
                (
                    entity["id"],
                    entity["path"],
                    entity["kind"],
                    entity["name"],
                    entity["parent_id"],
                    json.dumps(entity.get("payload", {}), ensure_ascii=False),
                ),
            )
            if entity["parent_id"] is not None and entity["kind"] != "test":
                connection.execute(
                    "INSERT OR IGNORE INTO code_relations(source_id, target_id, kind) "
                    "VALUES (?, ?, 'contains')",
                    (entity["parent_id"], entity["id"]),
                )
        for relation in relations:
            self._insert_relation(connection, relation)

    def _rebuild_relations_for_paths(
        self,
        connection: sqlite3.Connection,
        rel_paths: list[str],
        test_dirs: set[str],
    ) -> None:
        self._delete_outgoing_non_contains_relations_for_paths(connection, rel_paths)
        scanned = WorkspaceOverviewService.scan_paths(self.project_root, rel_paths)
        file_entities = self._workspace_file_entities(connection, rel_paths, scanned)
        index = self._indexer.run(
            self.project_root,
            file_entities=file_entities,
            parse_paths=rel_paths,
            test_dirs=test_dirs,
        )
        for relation in index.relations:
            self._insert_relation(connection, relation)
        nontest_files = {
            entity["path"] for entity in file_entities if entity.get("kind") in ("file",)
        }
        scip = self._scip.index_paths(
            self.project_root,
            rel_paths,
            file_entities=[entity for entity in file_entities if entity.get("kind") == "file"],
            nontest_files=nontest_files,
        )
        self._replace_scip_for_paths(connection, rel_paths, scip)
        for relation in scip.relations:
            self._insert_relation(connection, relation)
        blind_rows = self._collect_blind_spots(rel_paths, nontest_files)
        self._upsert_blind_spots_for_files(connection, rel_paths, blind_rows)

    @staticmethod
    def _replace_scip_for_paths(connection: sqlite3.Connection, rel_paths: list[str], scip) -> None:
        for rel_path in rel_paths:
            connection.execute("DELETE FROM code_occurrences WHERE file_path = ?", (rel_path,))
            connection.execute("DELETE FROM code_symbols WHERE moniker GLOB ?", (f"{rel_path}::*",))
        ProjectPlanStore._upsert_scip_data(connection, scip)

    def _assert_patch_mutable(self, connection: sqlite3.Connection, patch: PlanPatchSchema) -> None:
        """Guard running semantics; patch input either exits cleanly or raises a structured conflict."""
        direct_ids = {
            *(item.id for item in patch.update_nodes),
            *patch.remove_node_ids,
            *(item.node_id for item in patch.replace_dependencies),
        }
        running_direct = self._running_ids(connection, direct_ids)
        running_dependents: set[str] = set()
        for removed_id in patch.remove_node_ids:
            rows = connection.execute(
                "SELECT n.id FROM plan_nodes n JOIN plan_edges e ON e.source_id = n.id "
                "WHERE n.status = 'running' AND n.archived = 0 "
                "AND e.kind = 'depends_on' AND e.target_id = ? "
                "UNION SELECT id FROM plan_nodes WHERE status = 'running' "
                "AND archived = 0 AND parent_id = ?",
                (removed_id, removed_id),
            ).fetchall()
            running_dependents.update(str(row[0]) for row in rows)
        affected = running_direct | running_dependents
        if affected:
            raise ConflictError(
                resource="plan_node",
                message="Running nodes and their execution semantics are immutable",
                details={"node_ids": sorted(affected)},
                error_code="node_running_immutable",
            )

    def _apply_patch(self, connection: sqlite3.Connection, patch: PlanPatchSchema) -> set[str]:
        """Execute local SQL mutations; connection/patch input returns all affected node IDs."""
        changed: set[str] = set()
        for node in patch.add_nodes:
            if connection.execute("SELECT 1 FROM plan_nodes WHERE id = ?", (node.id,)).fetchone():
                raise ConflictError(
                    resource="plan_node",
                    message="Node ID already exists",
                    details={"node_id": node.id},
                    error_code="node_already_exists",
                )
            raw = node.model_dump(mode="json")
            payload = {
                key: value
                for key, value in raw.items()
                if key not in {"id", "parent_id", "order", "depends_on", "node_type", "title", "goal"}
            }
            connection.execute(
                "INSERT INTO plan_nodes(id, parent_id, node_order, status, node_type, title, goal, payload) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    node.id,
                    node.parent_id,
                    node.order,
                    node.node_type,
                    node.title,
                    node.goal,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            for dependency in node.depends_on:
                connection.execute(
                    "INSERT INTO plan_edges(source_id, target_id, kind) VALUES (?, ?, 'depends_on')",
                    (node.id, dependency),
                )
            self._record_change(connection, "plan_node", node.id, "add")
            changed.add(node.id)

        for update in patch.update_nodes:
            row = connection.execute(
                "SELECT * FROM plan_nodes WHERE id = ? AND archived = 0", (update.id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(resource="plan_node", details={"node_id": update.id})
            values = update.model_dump(mode="json", exclude_unset=True)
            values.pop("id", None)
            structured = {
                "parent_id": "parent_id",
                "order": "node_order",
                "node_type": "node_type",
                "title": "title",
                "goal": "goal",
            }
            assignments: list[str] = []
            parameters: list[Any] = []
            for field, column in structured.items():
                if field in values:
                    assignments.append(f"{column} = ?")
                    parameters.append(values.pop(field))
            if values:
                json_arguments: list[str] = []
                for field, value in values.items():
                    json_arguments.extend(["?", "json(?)"])
                    parameters.extend([
                        f"$.{field}",
                        json.dumps(value, ensure_ascii=False),
                    ])
                assignments.append(f"payload = json_set(payload, {', '.join(json_arguments)})")
            if assignments:
                if row["status"] in {"completed", "failed", "blocked"}:
                    assignments.append("status = 'pending'")
                parameters.append(update.id)
                connection.execute(
                    f"UPDATE plan_nodes SET {', '.join(assignments)} WHERE id = ?", parameters,
                )
            self._record_change(connection, "plan_node", update.id, "update")
            changed.add(update.id)

        for replacement in patch.replace_dependencies:
            if connection.execute(
                "SELECT 1 FROM plan_nodes WHERE id = ? AND archived = 0", (replacement.node_id,),
            ).fetchone() is None:
                raise NotFoundError(resource="plan_node", details={"node_id": replacement.node_id})
            connection.execute(
                "DELETE FROM plan_edges WHERE source_id = ? AND kind = 'depends_on'",
                (replacement.node_id,),
            )
            for dependency in replacement.depends_on:
                connection.execute(
                    "INSERT INTO plan_edges(source_id, target_id, kind) VALUES (?, ?, 'depends_on')",
                    (replacement.node_id, dependency),
                )
            self._record_change(connection, "plan_edge", replacement.node_id, "replace_dependencies")
            changed.add(replacement.node_id)

        for node_id in patch.remove_node_ids:
            result = connection.execute(
                "UPDATE plan_nodes SET archived = 1 WHERE id = ? AND archived = 0", (node_id,),
            )
            if result.rowcount == 0:
                raise NotFoundError(resource="plan_node", details={"node_id": node_id})
            connection.execute("DELETE FROM plan_edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))
            self._record_change(connection, "plan_node", node_id, "remove")
            changed.add(node_id)
        return changed

    def _validate_graph(self, connection: sqlite3.Connection) -> None:
        """Validate hierarchy/dependency references and cycles; connection input exits or rolls back patch."""
        node_ids = {
            str(row[0])
            for row in connection.execute("SELECT id FROM plan_nodes WHERE archived = 0").fetchall()
        }
        parent_rows = connection.execute(
            "SELECT id, parent_id FROM plan_nodes WHERE archived = 0 AND parent_id IS NOT NULL"
        ).fetchall()
        for row in parent_rows:
            if row["parent_id"] not in node_ids:
                raise ValidationError(
                    resource="plan_node",
                    message="Node parent does not exist",
                    details={"node_id": row["id"], "parent_id": row["parent_id"]},
                )
        dependency_rows = connection.execute(
            "SELECT source_id, target_id FROM plan_edges WHERE kind = 'depends_on'"
        ).fetchall()
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for row in dependency_rows:
            if row["source_id"] not in node_ids or row["target_id"] not in node_ids:
                raise ValidationError(
                    resource="plan_edge",
                    message="Dependency endpoint does not exist",
                    details={"source_id": row["source_id"], "target_id": row["target_id"]},
                )
            adjacency[row["source_id"]].append(row["target_id"])
        self._assert_acyclic(adjacency, "dependency_cycle")
        hierarchy = {str(row["id"]): [str(row["parent_id"])] for row in parent_rows}
        self._assert_acyclic(hierarchy, "hierarchy_cycle")

    @staticmethod
    def _assert_acyclic(adjacency: dict[str, list[str]], error_code: str) -> None:
        """Detect a directed cycle; adjacency/error input exits or raises validation error."""
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            """Depth-first one node; node input marks it visited or raises on a back edge."""
            if node_id in visiting:
                raise ValidationError(
                    resource="plan_graph",
                    message="Plan graph contains a cycle",
                    details={"error_code": error_code, "node_id": node_id},
                )
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in adjacency.get(node_id, []):
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in adjacency:
            visit(node_id)

    def _row_to_node(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """Deserialize one DB row; connection/row input returns the public node dictionary."""
        payload = json.loads(row["payload"])
        dependencies = [
            str(item[0])
            for item in connection.execute(
                "SELECT target_id FROM plan_edges WHERE source_id = ? AND kind = 'depends_on' "
                "ORDER BY target_id",
                (row["id"],),
            ).fetchall()
        ]
        return {
            **payload,
            "id": row["id"],
            "parent_id": row["parent_id"],
            "order": row["node_order"],
            "status": row["status"],
            "node_type": row["node_type"],
            "title": row["title"],
            "goal": row["goal"],
            "depends_on": dependencies,
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        """Deserialize an event row; row input returns JSON-safe event data."""
        return {
            "change_seq": row["change_seq"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "operation": row["operation"],
            "payload": json.loads(row["payload"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _objection_from_row(row: sqlite3.Row) -> dict[str, Any]:
        """Deserialize one objection row for arbitration APIs."""
        return {
            "id": row["id"],
            "objection_type": row["objection_type"],
            "related_node_ids": json.loads(row["related_node_ids"]),
            "evidence": json.loads(row["evidence"]),
            "suggested_resolution": json.loads(row["suggested_resolution"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    @staticmethod
    def _code_entity_from_row(row: sqlite3.Row) -> dict[str, Any]:
        """Deserialize a code row; row input returns the public entity dictionary."""
        return {
            "id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "name": row["name"],
            "parent_id": row["parent_id"],
            "payload": json.loads(row["payload"]),
        }

    @staticmethod
    def _record_change(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append a safe event; connection/entity/operation input produces one change_seq row."""
        connection.execute(
            "INSERT INTO change_events(entity_type, entity_id, operation, payload) VALUES (?, ?, ?, ?)",
            (entity_type, entity_id, operation, json.dumps(payload or {}, ensure_ascii=False)),
        )

    @staticmethod
    def _running_ids(connection: sqlite3.Connection, node_ids: Iterable[str]) -> set[str]:
        """Find running IDs; connection/ID input returns the active immutable subset."""
        ids = sorted(set(node_ids))
        if not ids:
            return set()
        rows = connection.execute(
            f"SELECT id FROM plan_nodes WHERE archived = 0 AND status = 'running' "
            f"AND id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _count(self, table: str) -> int:
        """Count a known table; internal table-name input returns its row count."""
        if table not in {"code_entities", "plan_nodes"}:
            raise ValueError("Unsupported count table")
        with self._connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _latest_change_seq(self, connection: sqlite3.Connection | None = None) -> int:
        """Read the current event sequence; optional connection input returns an integer watermark."""
        if connection is not None:
            return int(connection.execute("SELECT COALESCE(MAX(change_seq), 0) FROM change_events").fetchone()[0])
        with self._connect() as owned:
            return self._latest_change_seq(owned)

    @staticmethod
    def _patch_size(patch: PlanPatchSchema) -> int:
        """Count patch operations; schema input returns a non-sensitive integer for logs."""
        return (
            len(patch.add_nodes)
            + len(patch.update_nodes)
            + len(patch.remove_node_ids)
            + len(patch.replace_dependencies)
        )

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        """Clamp a requested page size; integer input returns a 1..MAX_PAGE_LIMIT bound."""
        return max(1, min(int(limit), MAX_PAGE_LIMIT))

    @staticmethod
    def _encode_cursor(values: list[Any]) -> str:
        """Encode stable cursor values; JSON-safe list input returns URL-safe opaque text."""
        raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> list[Any]:
        """Decode an opaque cursor; text input returns its list or a structured validation error."""
        if cursor is None:
            return []
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
            values = json.loads(decoded.decode("utf-8"))
            if not isinstance(values, list):
                raise ValueError
            return values
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(resource="cursor", message="Invalid cursor") from exc

    def _decode_order_cursor(self, cursor: str | None) -> tuple[int, str]:
        """Decode a node cursor; optional text input returns order and ID defaults."""
        values = self._decode_cursor(cursor)
        if not values:
            return -1, ""
        if len(values) != 2 or not isinstance(values[0], int) or not isinstance(values[1], str):
            raise ValidationError(resource="cursor", message="Invalid node cursor")
        return values[0], values[1]

    def _decode_path_cursor(self, cursor: str | None) -> tuple[str, str]:
        """Decode a code cursor; optional text input returns path and ID defaults."""
        values = self._decode_cursor(cursor)
        if not values:
            return "", ""
        if len(values) != 2 or not all(isinstance(value, str) for value in values):
            raise ValidationError(resource="cursor", message="Invalid code cursor")
        return values[0], values[1]

    def _decode_text_cursor(self, cursor: str | None) -> str:
        """Decode a search cursor; optional text input returns an ID default."""
        values = self._decode_cursor(cursor)
        if not values:
            return ""
        if len(values) != 1 or not isinstance(values[0], str):
            raise ValidationError(resource="cursor", message="Invalid search cursor")
        return values[0]

    def _log(
        self,
        action: str,
        status: str,
        *,
        node_id: str | None = None,
        duration_ms: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Emit one safe structured event; metadata input exits through the existing logging facade."""
        safe_detail = {"project_id": self.project_id, **(detail or {})}
        self._facade.info_event(
            action,
            status,
            node_id=node_id,
            project_id=self.project_id,
            duration_ms=duration_ms,
            detail=safe_detail,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """Convert a monotonic start; float input returns elapsed milliseconds."""
        return int((time.perf_counter() - started) * 1000)


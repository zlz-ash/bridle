from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from bridle.features.project_map.store import ProjectPlanStore


def test_wait_signal_ends_atomically_with_outbox(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="project-wait")
    store.ensure_schema()
    first = store.create_node_execution(
        node_id="node-wait",
        owner_address="agent://project-wait/main/1",
    )
    duplicate = store.create_node_execution(
        node_id="node-wait",
        owner_address="agent://project-wait/main/1",
    )

    assert first == duplicate
    assert first["state"] == "waiting"
    assert first["phase"] == "queued"
    assert first["outcome"] is None
    assert first["revision"] == 1
    assert {"wait_id", "execution_id", "node_id"} <= first.keys()

    ended = store.complete_execution(
        wait_id=first["wait_id"],
        outcome="completed",
        result_ref=".bridle/results/node-wait.json",
    )
    repeated = store.complete_execution(
        wait_id=first["wait_id"],
        outcome="completed",
        result_ref=".bridle/results/node-wait.json",
    )
    assert ended == repeated
    assert ended["state"] == "ended"
    assert ended["outcome"] == "completed"
    assert ended["result_ref"] == ".bridle/results/node-wait.json"

    with closing(sqlite3.connect(store.database_path)) as connection:
        wait_row = connection.execute(
            "SELECT state, outcome, result_ref FROM wait_signals WHERE wait_id = ?",
            (first["wait_id"],),
        ).fetchone()
        execution_row = connection.execute(
            "SELECT state, outcome, result_ref FROM node_executions WHERE execution_id = ?",
            (first["execution_id"],),
        ).fetchone()
        outbox_rows = connection.execute(
            "SELECT wait_id, state FROM completion_outbox WHERE wait_id = ?",
            (first["wait_id"],),
        ).fetchall()
    assert wait_row == ("ended", "completed", ".bridle/results/node-wait.json")
    assert execution_row == ("ended", "completed", ".bridle/results/node-wait.json")
    assert outbox_rows == [(first["wait_id"], "pending")]

    assert store.read_execution(first["wait_id"]) == ended
    assert store.read_execution(first["wait_id"])["state"] == "ended"

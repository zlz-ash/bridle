"""Default semantic scan: route open blind spots into reviewable annotations."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bridle.features.project_map.store import ProjectPlanStore

DEFAULT_BLIND_SPOT_BUDGET = 50


class SemanticScanService:
    """Deterministic semantic phase: route open blind spots, never mutate code_relations."""

    def __init__(self, *, budget: int = DEFAULT_BLIND_SPOT_BUDGET) -> None:
        self._budget = max(1, min(budget, 200))

    def run(self, store: "ProjectPlanStore") -> dict[str, Any]:
        processed = 0
        routed = 0
        deferred = 0
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, file_path, detail FROM map_blind_spots "
                "WHERE status = 'open' ORDER BY file_path, id LIMIT ?",
                (self._budget,),
            ).fetchall()
            for row in rows:
                spot_id = str(row["id"])
                file_path = str(row["file_path"] or "")
                if not file_path:
                    connection.execute(
                        "UPDATE map_blind_spots SET status = 'deferred' WHERE id = ?",
                        (spot_id,),
                    )
                    deferred += 1
                    processed += 1
                    continue
                entity = connection.execute(
                    "SELECT id FROM code_entities WHERE path = ? OR path LIKE ? "
                    "ORDER BY CASE WHEN path = ? THEN 0 ELSE 1 END LIMIT 1",
                    (file_path, f"{file_path}::%", file_path),
                ).fetchone()
                if entity is None:
                    connection.execute(
                        "UPDATE map_blind_spots SET status = 'deferred' WHERE id = ?",
                        (spot_id,),
                    )
                    deferred += 1
                    processed += 1
                    continue
                detail = json.loads(row["detail"] or "{}")
                summary = f"Blind spot ({row['kind']}) in {file_path} requires semantic review"
                store._route_blind_spot_to_review(
                    connection,
                    spot_id=spot_id,
                    source_id=str(entity["id"]),
                    file_path=file_path,
                    summary=summary,
                    evidence={"blind_spot_kind": str(row["kind"]), "detail": detail},
                )
                routed += 1
                processed += 1
            remaining = int(
                connection.execute(
                    "SELECT COUNT(*) FROM map_blind_spots WHERE status = 'open'"
                ).fetchone()[0]
            )
        return {
            "processed": processed,
            "routed": routed,
            "deferred": deferred,
            "remaining": remaining,
        }

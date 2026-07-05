"""Bounded progressive map queries for agent tools."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from bridle.api.errors import ValidationError
from bridle.features.workspace.overview_service import WorkspaceOverviewService

DEFAULT_MAX_NODES = 50
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_TOKENS = 8000
MAPPING_SEED_MAX_DEPTH = 2
MAPPING_SEED_MAX_NODES = 200
SUPPORTED_RISKS = frozenset({"low", "medium", "high"})


class MapQueryService:
    """Progressive code-map reads with budget enforcement."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()

    def get_node(self, connection: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM code_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(resource="code_entity", message="Entity not found", details={"id": entity_id})
        return self._entity_row(row)

    def neighbors(
        self,
        connection: sqlite3.Connection,
        entity_id: str,
        *,
        kinds: list[str] | None = None,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> dict[str, Any]:
        limit = max(1, min(max_nodes, 200))
        kind_filter = kinds or []
        params: list[Any] = [entity_id, entity_id]
        kind_clause = ""
        if kind_filter:
            placeholders = ",".join("?" for _ in kind_filter)
            kind_clause = f" AND kind IN ({placeholders})"
            params.extend(kind_filter)
        params.append(limit)
        rows = connection.execute(
            f"SELECT * FROM code_entities WHERE id IN ("
            f"SELECT target_id FROM code_relations WHERE source_id = ? "
            f"UNION SELECT source_id FROM code_relations WHERE target_id = ?"
            f"){kind_clause} LIMIT ?",
            params,
        ).fetchall()
        return {
            "center_id": entity_id,
            "items": [self._entity_row(row) for row in rows],
            "truncated": len(rows) >= limit,
        }

    def subgraph(
        self,
        connection: sqlite3.Connection,
        entity_id: str,
        *,
        depth: int = 1,
        max_nodes: int = DEFAULT_MAX_NODES,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        bounded_depth = max(0, min(depth, 5))
        limit = max(1, min(max_nodes, 200))
        seen = {entity_id}
        frontier = {entity_id}
        for _ in range(bounded_depth):
            if len(seen) >= limit:
                break
            next_frontier: set[str] = set()
            for node_id in frontier:
                rows = connection.execute(
                    "SELECT target_id FROM code_relations WHERE source_id = ? "
                    "UNION SELECT source_id FROM code_relations WHERE target_id = ?",
                    (node_id, node_id),
                ).fetchall()
                for row in rows:
                    neighbor = str(row[0])
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    next_frontier.add(neighbor)
                    if len(seen) >= limit:
                        break
                if len(seen) >= limit:
                    break
            frontier = next_frontier

        kind_filter = kinds or []
        placeholders = ",".join("?" for _ in seen)
        params: list[Any] = list(sorted(seen))
        kind_clause = ""
        if kind_filter:
            kind_placeholders = ",".join("?" for _ in kind_filter)
            kind_clause = f" AND kind IN ({kind_placeholders})"
            params.extend(kind_filter)
        rows = connection.execute(
            f"SELECT * FROM code_entities WHERE id IN ({placeholders}){kind_clause}",
            params,
        ).fetchall()
        edge_rows = connection.execute(
            f"SELECT source_id, target_id, kind FROM code_relations "
            f"WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders})",
            (*sorted(seen), *sorted(seen)),
        ).fetchall()
        return {
            "center_id": entity_id,
            "nodes": [self._entity_row(row) for row in rows],
            "edges": [dict(row) for row in edge_rows],
            "truncated": len(seen) >= limit,
        }

    def read_span(
        self,
        connection: sqlite3.Connection,
        entity_id: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT path, payload FROM code_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(resource="code_entity", message="Entity not found", details={"id": entity_id})
        path = str(row["path"])
        if "::" in path:
            file_path, _symbol = path.split("::", 1)
        else:
            file_path = path
        payload = json.loads(row["payload"]) if row["payload"] else {}
        target = self.project_root.joinpath(*file_path.split("/"))
        if not target.is_file():
            return {"entity_id": entity_id, "content": "", "truncated": False}
        text = target.read_text(encoding="utf-8", errors="replace")
        char_budget = max(500, min(max_tokens, 32000)) * 4
        truncated = len(text) > char_budget
        content = text[:char_budget]
        range_info = payload.get("range")
        if isinstance(range_info, dict) and "start_line" in range_info:
            lines = text.splitlines()
            start = max(0, int(range_info["start_line"]) - 1)
            end = min(len(lines), int(range_info.get("end_line", start + 1)))
            snippet = "\n".join(lines[start:end])
            if len(snippet) <= char_budget:
                content = snippet
                truncated = False
        return {"entity_id": entity_id, "path": file_path, "content": content, "truncated": truncated}

    def blind_spots(
        self,
        connection: sqlite3.Connection,
        *,
        seed_id: str | None = None,
        status: str = "open",
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> dict[str, Any]:
        limit = max(1, min(max_nodes, 200))
        if seed_id:
            row = connection.execute(
                "SELECT file_path FROM map_blind_spots WHERE id = ? AND status = ?",
                (seed_id, status),
            ).fetchone()
            if row is None:
                raise ValidationError(
                    resource="map_blind_spot",
                    message="Blind spot seed not found or not open",
                    details={"seed_id": seed_id},
                )
            rows = connection.execute(
                "SELECT * FROM map_blind_spots WHERE status = ? AND file_path = ? LIMIT ?",
                (status, row["file_path"], limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM map_blind_spots WHERE status = ? LIMIT ?",
                (status, limit),
            ).fetchall()
        return {
            "items": [self._blind_spot_row(row) for row in rows],
            "truncated": len(rows) >= limit,
        }

    def require_blind_spot_seed(self, connection: sqlite3.Connection, seed_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM map_blind_spots WHERE id = ? AND status = 'open'",
            (seed_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(
                resource="map_blind_spot",
                message="Mapping queries require an open blind spot seed",
                details={"seed_id": seed_id},
            )
        return self._blind_spot_row(row)

    def seed_allowed_entity_ids(
        self,
        connection: sqlite3.Connection,
        seed_id: str,
        *,
        max_depth: int = MAPPING_SEED_MAX_DEPTH,
        max_nodes: int = MAPPING_SEED_MAX_NODES,
    ) -> set[str]:
        seed = self.require_blind_spot_seed(connection, seed_id)
        file_path = str(seed["file_path"])
        allowed: set[str] = {self.file_entity_id(file_path)}
        rows = connection.execute(
            "SELECT id FROM code_entities WHERE path = ? OR path LIKE ?",
            (file_path, f"{file_path}::%"),
        ).fetchall()
        allowed.update(str(row[0]) for row in rows)
        frontier = set(allowed)
        bounded_depth = max(0, min(max_depth, 5))
        for _ in range(bounded_depth):
            if len(allowed) >= max_nodes:
                break
            next_frontier: set[str] = set()
            for entity_id in frontier:
                neighbor_rows = connection.execute(
                    "SELECT target_id FROM code_relations WHERE source_id = ? "
                    "UNION SELECT source_id FROM code_relations WHERE target_id = ?",
                    (entity_id, entity_id),
                ).fetchall()
                for row in neighbor_rows:
                    neighbor_id = str(row[0])
                    if neighbor_id in allowed:
                        continue
                    allowed.add(neighbor_id)
                    next_frontier.add(neighbor_id)
                    if len(allowed) >= max_nodes:
                        break
                if len(allowed) >= max_nodes:
                    break
            frontier = next_frontier
            if not frontier:
                break
        return allowed

    def assert_entity_in_seed_scope(
        self,
        connection: sqlite3.Connection,
        seed_id: str,
        entity_id: str,
    ) -> None:
        allowed = self.seed_allowed_entity_ids(connection, seed_id)
        if entity_id not in allowed:
            raise ValidationError(
                resource="map_blind_spot",
                message="Entity is outside mapping seed neighborhood",
                details={"seed_id": seed_id, "entity_id": entity_id},
            )

    def filter_to_seed_scope(
        self,
        connection: sqlite3.Connection,
        seed_id: str,
        entity_ids: Iterable[str],
    ) -> list[str]:
        allowed = self.seed_allowed_entity_ids(connection, seed_id)
        return [entity_id for entity_id in entity_ids if entity_id in allowed]

    @staticmethod
    def _entity_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "name": row["name"],
            "parent_id": row["parent_id"],
            "payload": json.loads(row["payload"]),
        }

    @staticmethod
    def _blind_spot_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "file_path": row["file_path"],
            "range": json.loads(row["range"] or "{}"),
            "detail": json.loads(row["detail"] or "{}"),
            "source": row["source"],
            "status": row["status"],
        }

    @staticmethod
    def file_entity_id(rel_path: str) -> str:
        return WorkspaceOverviewService._entity_id("file", rel_path)

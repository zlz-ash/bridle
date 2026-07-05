"""Git co-change mining, module metrics, and directory-prior clustering."""
from __future__ import annotations

import sqlite3
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _module_for_path(rel_path: str) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) > 1:
        return parts[0]
    return "."


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class BoundaryService:
    """Co-change edges, module metrics, and conflict detection."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()

    def refresh_cochange(self, connection: sqlite3.Connection) -> int:
        """Mine git history; output is the number of co-change rows upserted."""
        try:
            output = subprocess.check_output(
                ["git", "log", "--name-only", "--pretty=format:%H", "HEAD"],
                cwd=self.project_root,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return 0

        commits: list[list[str]] = []
        current: list[str] = []
        for line in output.splitlines():
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line.lower()):
                if current:
                    commits.append(current)
                current = []
            elif line.strip():
                current.append(line.strip().replace("\\", "/"))
        if current:
            commits.append(current)

        co_counts: dict[tuple[str, str], int] = defaultdict(int)
        sup: dict[str, int] = defaultdict(int)
        for files in commits:
            unique = sorted(set(files))
            for path in unique:
                sup[path] += 1
            for i, a in enumerate(unique):
                for b in unique[i + 1 :]:
                    co_counts[_pair_key(a, b)] += 1

        now = datetime.now(UTC).isoformat()
        connection.execute("DELETE FROM code_cochange")
        for (a, b), co in co_counts.items():
            sa, sb = sup[a], sup[b]
            denom = sa + sb - co
            weight = co / denom if denom else 0.0
            connection.execute(
                "INSERT INTO code_cochange(path_a, path_b, co_count, sup_a, sup_b, weight, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (a, b, co, sa, sb, weight, now),
            )
        return len(co_counts)

    def compute_metrics(self, connection: sqlite3.Connection, *, change_seq: int | None = None) -> int:
        """Recompute module_metrics from structural import edges."""
        modules: set[str] = set()
        rows = connection.execute(
            "SELECT path FROM code_entities WHERE kind IN ('file', 'function', 'class', 'method')"
        ).fetchall()
        for row in rows:
            path = str(row["path"]).split("::", 1)[0]
            modules.add(_module_for_path(path))

        import_edges = connection.execute(
            "SELECT source_id, target_id FROM code_relations WHERE kind = 'imports'"
        ).fetchall()
        entity_paths = {
            str(r["id"]): str(r["path"]).split("::", 1)[0]
            for r in connection.execute("SELECT id, path FROM code_entities").fetchall()
        }

        ca: dict[str, int] = defaultdict(int)
        ce: dict[str, int] = defaultdict(int)
        for edge in import_edges:
            src_path = entity_paths.get(str(edge["source_id"]), "")
            tgt_path = entity_paths.get(str(edge["target_id"]), "")
            src_mod = _module_for_path(src_path)
            tgt_mod = _module_for_path(tgt_path)
            if src_mod != tgt_mod:
                ce[src_mod] += 1
                ca[tgt_mod] += 1

        now = datetime.now(UTC).isoformat()
        connection.execute("DELETE FROM module_metrics")
        count = 0
        for module_id in modules:
            incoming = ca.get(module_id, 0)
            outgoing = ce.get(module_id, 0)
            instability = outgoing / (incoming + outgoing) if (incoming + outgoing) else 0.0
            metrics = {
                "ca": float(incoming),
                "ce": float(outgoing),
                "instability": instability,
                "interface_width": float(outgoing),
                "lcom": 1.0,
                "relational_cohesion": 0.5,
            }
            for name, value in metrics.items():
                connection.execute(
                    "INSERT INTO module_metrics(module_id, metric, value, change_seq, computed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (module_id, name, value, change_seq, now),
                )
                count += 1
        return count

    def list_boundary_conflicts(self, connection: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
        """Top-N directory vs co-change conflicts for human review."""
        rows = connection.execute(
            "SELECT path_a, path_b, weight FROM code_cochange ORDER BY weight DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            mod_a = _module_for_path(str(row["path_a"]))
            mod_b = _module_for_path(str(row["path_b"]))
            if mod_a != mod_b:
                conflicts.append(
                    {
                        "path_a": row["path_a"],
                        "path_b": row["path_b"],
                        "module_a": mod_a,
                        "module_b": mod_b,
                        "weight": row["weight"],
                        "reason": "directory_vs_cochange",
                    }
                )
        return conflicts[:limit]

    def cluster_modules(self, connection: sqlite3.Connection) -> dict[str, list[str]]:
        """Directory-prior clustering; high co-change cross-dir pairs become debt hints."""
        modules: dict[str, list[str]] = defaultdict(list)
        rows = connection.execute(
            "SELECT path FROM code_entities WHERE kind = 'file'"
        ).fetchall()
        for row in rows:
            path = str(row["path"])
            mod = _module_for_path(path)
            modules[mod].append(path)
        return dict(modules)

    def debt_node_ids(self, connection: sqlite3.Connection, *, threshold: float = 0.5) -> list[str]:
        """Nodes entangled across module boundaries (high co-change, different dirs)."""
        conflicts = self.list_boundary_conflicts(connection, limit=20)
        debt: list[str] = []
        for item in conflicts:
            if float(item["weight"]) >= threshold:
                debt.append(f"debt:{item['path_a']}:{item['path_b']}")
        return debt

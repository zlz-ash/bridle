"""Deterministic semantic-map synthesis from the structural code map."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_KINDS = {"file"}


def _module_for_path(rel_path: str) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) > 1:
        return parts[0]
    return "."


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\n".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:24]}"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().replace("/", "-"))
    return slug.strip("-")[:80] or "root"


class SemanticSynthesisService:
    """Build module candidates, evidence bundles, and interface mock artifacts."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def synthesize(self, connection: sqlite3.Connection, *, run_id: str) -> dict[str, Any]:
        """Read structure rows and return deterministic semantic-map candidate rows."""
        entities = [dict(row) for row in connection.execute("SELECT * FROM code_entities").fetchall()]
        entity_by_id = {str(row["id"]): row for row in entities}
        file_entities = [
            row for row in entities
            if str(row.get("kind")) in SOURCE_KINDS and "::" not in str(row.get("path", ""))
        ]
        relations = [dict(row) for row in connection.execute("SELECT * FROM code_relations").fetchall()]
        metrics = [dict(row) for row in connection.execute("SELECT * FROM module_metrics").fetchall()]
        blind_spots = [dict(row) for row in connection.execute("SELECT * FROM map_blind_spots").fetchall()]
        cochange = [dict(row) for row in connection.execute("SELECT * FROM code_cochange").fetchall()]

        file_hashes = {
            str(row["path"]): self._file_hash(str(row["path"]))
            for row in file_entities
        }
        groups: dict[str, list[str]] = defaultdict(list)
        for row in file_entities:
            path = str(row["path"])
            groups[_module_for_path(path)].append(path)

        evidence_payload = self._evidence_payload(
            file_entities=file_entities,
            relations=relations,
            entity_by_id=entity_by_id,
            metrics=metrics,
            blind_spots=blind_spots,
            cochange=cochange,
        )
        evidence_id = _stable_id("evidence", run_id, json.dumps(evidence_payload, sort_keys=True, default=str))
        candidates = self._module_candidates(run_id, groups, file_hashes, evidence_id, metrics, relations, entity_by_id)
        candidate_by_module = {item["module_id"]: item for item in candidates}
        candidate_files = self._candidate_files(candidates, groups, file_hashes, evidence_id)
        module_edges = self._module_edges(run_id, relations, entity_by_id, candidate_by_module, evidence_id)
        interface_candidates, mock_artifacts = self._interface_candidates(
            run_id,
            module_edges,
            candidate_by_module,
            evidence_id,
        )

        return {
            "evidence": {
                "id": evidence_id,
                "run_id": run_id,
                "kind": "structure_bundle",
                "payload": evidence_payload,
            },
            "module_candidates": candidates,
            "module_candidate_files": candidate_files,
            "module_edges": module_edges,
            "module_interface_candidates": interface_candidates,
            "interface_mock_artifacts": mock_artifacts,
        }

    def _evidence_payload(
        self,
        *,
        file_entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        entity_by_id: dict[str, dict[str, Any]],
        metrics: list[dict[str, Any]],
        blind_spots: list[dict[str, Any]],
        cochange: list[dict[str, Any]],
    ) -> dict[str, Any]:
        relation_counts: dict[str, int] = defaultdict(int)
        cross_module: list[dict[str, Any]] = []
        for relation in relations:
            kind = str(relation.get("kind", ""))
            relation_counts[kind] += 1
            src = entity_by_id.get(str(relation.get("source_id")))
            tgt = entity_by_id.get(str(relation.get("target_id")))
            if not src or not tgt:
                continue
            src_path = str(src.get("path", "")).split("::", 1)[0]
            tgt_path = str(tgt.get("path", "")).split("::", 1)[0]
            src_mod = _module_for_path(src_path)
            tgt_mod = _module_for_path(tgt_path)
            if src_mod != tgt_mod and kind != "contains":
                cross_module.append(
                    {
                        "kind": kind,
                        "source_id": relation.get("source_id"),
                        "target_id": relation.get("target_id"),
                        "source_path": src_path,
                        "target_path": tgt_path,
                        "source_module": src_mod,
                        "target_module": tgt_mod,
                    }
                )

        directory_groups: dict[str, int] = defaultdict(int)
        for row in file_entities:
            directory_groups[_module_for_path(str(row["path"]))] += 1

        return {
            "directory_groups": dict(sorted(directory_groups.items())),
            "relation_counts": dict(sorted(relation_counts.items())),
            "cross_module_relations": cross_module[:200],
            "metrics": [
                {
                    "module_id": row.get("module_id"),
                    "metric": row.get("metric"),
                    "value": row.get("value"),
                }
                for row in metrics
            ],
            "blind_spots": [
                {
                    "id": row.get("id"),
                    "kind": row.get("kind"),
                    "file_path": row.get("file_path"),
                    "status": row.get("status"),
                }
                for row in blind_spots
            ],
            "cochange": [
                {
                    "path_a": row.get("path_a"),
                    "path_b": row.get("path_b"),
                    "weight": row.get("weight"),
                }
                for row in cochange[:200]
            ],
        }

    def _module_candidates(
        self,
        run_id: str,
        groups: dict[str, list[str]],
        file_hashes: dict[str, str],
        evidence_id: str,
        metrics: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        entity_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        metrics_by_module: dict[str, dict[str, float]] = defaultdict(dict)
        for row in metrics:
            metrics_by_module[str(row.get("module_id"))][str(row.get("metric"))] = float(row.get("value") or 0.0)

        cross_counts: dict[str, int] = defaultdict(int)
        for relation in relations:
            if str(relation.get("kind")) == "contains":
                continue
            src = entity_by_id.get(str(relation.get("source_id")))
            tgt = entity_by_id.get(str(relation.get("target_id")))
            if not src or not tgt:
                continue
            src_mod = _module_for_path(str(src.get("path", "")).split("::", 1)[0])
            tgt_mod = _module_for_path(str(tgt.get("path", "")).split("::", 1)[0])
            if src_mod != tgt_mod:
                cross_counts[src_mod] += 1
                cross_counts[tgt_mod] += 1

        now = datetime.now(UTC).isoformat()
        candidates: list[dict[str, Any]] = []
        for module_id, files in sorted(groups.items()):
            sorted_files = sorted(files)
            identity = json.dumps({"module_id": module_id, "files": sorted_files}, sort_keys=True)
            confidence = 0.72 if module_id != "." else 0.55
            if len(sorted_files) == 1:
                confidence -= 0.05
            if cross_counts.get(module_id, 0):
                confidence -= min(0.12, cross_counts[module_id] * 0.02)
            confidence = max(0.35, min(0.95, confidence))
            candidates.append(
                {
                    "id": _stable_id("modcand", identity),
                    "run_id": run_id,
                    "module_id": module_id,
                    "name": "root" if module_id == "." else module_id,
                    "status": "candidate",
                    "confidence": confidence,
                    "evidence_id": evidence_id,
                    "metrics": {
                        **metrics_by_module.get(module_id, {}),
                        "file_count": float(len(sorted_files)),
                        "cross_relation_count": float(cross_counts.get(module_id, 0)),
                    },
                    "created_at": now,
                    "file_fingerprint": hashlib.sha256(
                        json.dumps(
                            [(path, file_hashes.get(path, "")) for path in sorted_files],
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            )
        return candidates

    def _candidate_files(
        self,
        candidates: list[dict[str, Any]],
        groups: dict[str, list[str]],
        file_hashes: dict[str, str],
        evidence_id: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            module_id = str(candidate["module_id"])
            for path in sorted(groups.get(module_id, [])):
                rows.append(
                    {
                        "candidate_id": candidate["id"],
                        "file_path": path,
                        "role": "implementation",
                        "file_hash": file_hashes.get(path, ""),
                        "evidence": {
                            "evidence_id": evidence_id,
                            "reason": "directory_prior",
                            "module_id": module_id,
                        },
                    }
                )
        return rows

    def _module_edges(
        self,
        run_id: str,
        relations: list[dict[str, Any]],
        entity_by_id: dict[str, dict[str, Any]],
        candidate_by_module: dict[str, dict[str, Any]],
        evidence_id: str,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for relation in relations:
            kind = str(relation.get("kind"))
            if kind == "contains":
                continue
            src = entity_by_id.get(str(relation.get("source_id")))
            tgt = entity_by_id.get(str(relation.get("target_id")))
            if not src or not tgt:
                continue
            src_path = str(src.get("path", "")).split("::", 1)[0]
            tgt_path = str(tgt.get("path", "")).split("::", 1)[0]
            src_mod = _module_for_path(src_path)
            tgt_mod = _module_for_path(tgt_path)
            if src_mod == tgt_mod:
                continue
            src_candidate = candidate_by_module.get(src_mod)
            tgt_candidate = candidate_by_module.get(tgt_mod)
            if not src_candidate or not tgt_candidate:
                continue
            key = (str(src_candidate["id"]), str(tgt_candidate["id"]), kind)
            item = grouped.setdefault(
                key,
                {
                    "id": _stable_id("modedge", run_id, *key),
                    "run_id": run_id,
                    "source_candidate_id": src_candidate["id"],
                    "target_candidate_id": tgt_candidate["id"],
                    "source_module": src_mod,
                    "target_module": tgt_mod,
                    "kind": kind,
                    "weight": 0.0,
                    "evidence": {"evidence_id": evidence_id, "relations": []},
                },
            )
            item["weight"] = float(item["weight"]) + 1.0
            item["evidence"]["relations"].append(
                {
                    "source_id": relation.get("source_id"),
                    "target_id": relation.get("target_id"),
                    "source_path": src_path,
                    "target_path": tgt_path,
                }
            )
        return list(grouped.values())

    def _interface_candidates(
        self,
        run_id: str,
        module_edges: list[dict[str, Any]],
        candidate_by_module: dict[str, dict[str, Any]],
        evidence_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        now = datetime.now(UTC).isoformat()
        candidates: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        for edge in module_edges:
            if edge["kind"] != "imports":
                continue
            source_module = str(edge["source_module"])
            target_module = str(edge["target_module"])
            symbol = f"{target_module}_boundary"
            candidate_id = _stable_id("ifacecand", source_module, target_module, symbol)
            mock_rel = (
                ".bridle/semantic-map/mocks/"
                f"{_safe_slug(source_module)}__to__{_safe_slug(target_module)}__{_safe_slug(symbol)}.json"
            )
            signature = {
                "kind": "module_import_boundary",
                "provider_module": target_module,
                "consumer_module": source_module,
                "symbol": symbol,
            }
            relations = sorted(
                edge["evidence"].get("relations", []),
                key=lambda item: (
                    str(item.get("source_path", "")),
                    str(item.get("target_path", "")),
                    str(item.get("source_id", "")),
                    str(item.get("target_id", "")),
                ),
            )
            evidence = {
                "evidence_id": evidence_id,
                "module_edge_id": edge["id"],
                "relations": relations,
            }
            payload = {
                "interface_candidate_id": candidate_id,
                "from_module": target_module,
                "to_module": source_module,
                "symbol": symbol,
                "signature": signature,
                "evidence": {"relations": relations},
                "status": "candidate",
            }
            mock_hash = self._write_mock_artifact(mock_rel, payload)
            candidates.append(
                {
                    "id": candidate_id,
                    "run_id": run_id,
                    "from_module": target_module,
                    "to_module": source_module,
                    "from_candidate_id": candidate_by_module[target_module]["id"],
                    "to_candidate_id": candidate_by_module[source_module]["id"],
                    "symbol": symbol,
                    "signature": signature,
                    "evidence": evidence,
                    "mock_file_path": mock_rel,
                    "mock_hash": mock_hash,
                    "confidence": 0.66,
                    "status": "candidate",
                    "created_at": now,
                }
            )
            artifacts.append(
                {
                    "id": _stable_id("mock", candidate_id, mock_hash),
                    "interface_candidate_id": candidate_id,
                    "file_path": mock_rel,
                    "file_hash": mock_hash,
                    "status": "generated",
                    "payload": {
                        "mock": payload,
                        "artifact_metadata": {
                            "run_id": run_id,
                            "evidence": evidence,
                        },
                    },
                    "created_at": now,
                }
            )
        return candidates, artifacts

    def _write_mock_artifact(self, rel_path: str, payload: dict[str, Any]) -> str:
        target = self.project_root.joinpath(*rel_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(target)
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def _file_hash(self, rel_path: str) -> str:
        target = self.project_root.joinpath(*rel_path.split("/"))
        if not target.is_file():
            return ""
        return hashlib.sha256(target.read_bytes()).hexdigest()

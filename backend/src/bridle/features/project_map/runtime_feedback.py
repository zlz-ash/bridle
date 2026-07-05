"""Runtime execution feedback → blind spots + bounded reindex."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

MAX_RUNTIME_REINDEX_ATTEMPTS = 3

_IMPORT_ERROR = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_FILE_NOT_FOUND = re.compile(r"No such file or directory: ['\"]([^'\"]+)['\"]")


@dataclass
class RuntimeFeedbackResult:
    """Outcome of processing one execution failure."""

    blind_spot_ids: list[str]
    refresh_paths: list[str]
    reindex_attempts: int
    stopped_reason: str | None = None


class RuntimeFeedbackService:
    """Parse test/execution failures into runtime blind spots and trigger reindex."""

    def __init__(self) -> None:
        self._attempt_counts: dict[str, int] = {}

    def process_failure(
        self,
        *,
        execution_summary: str,
        test_summary: str,
        changed_paths: list[str],
    ) -> RuntimeFeedbackResult:
        """Extract missing paths; output is blind spot payloads and paths to refresh."""
        combined = f"{execution_summary}\n{test_summary}"
        missing: list[str] = []
        for pattern in (_IMPORT_ERROR, _FILE_NOT_FOUND):
            missing.extend(pattern.findall(combined))
        missing.extend(changed_paths)

        blind_ids: list[str] = []
        refresh: list[str] = []
        attempts = 0
        stopped: str | None = None

        for raw in sorted(set(missing)):
            normalized = raw.replace("\\", "/").strip("/")
            if not normalized:
                continue
            count = self._attempt_counts.get(normalized, 0) + 1
            self._attempt_counts[normalized] = count
            attempts = max(attempts, count)
            if count > MAX_RUNTIME_REINDEX_ATTEMPTS:
                stopped = "reindex_limit_reached"
                continue
            blind_ids.append(f"blind-{uuid.uuid4().hex}")
            refresh.append(normalized)

        return RuntimeFeedbackResult(
            blind_spot_ids=blind_ids,
            refresh_paths=sorted(set(refresh)),
            reindex_attempts=attempts,
            stopped_reason=stopped,
        )

    @staticmethod
    def blind_spot_rows(result: RuntimeFeedbackResult, *, file_paths: list[str]) -> list[dict[str, Any]]:
        """Build map_blind_spots rows aligned with blind_spot_ids."""
        rows: list[dict[str, Any]] = []
        for spot_id, path in zip(result.blind_spot_ids, file_paths, strict=False):
            rows.append(
                {
                    "id": spot_id,
                    "kind": "missing_edge",
                    "file_path": path,
                    "range": json.dumps({}),
                    "detail": json.dumps({"reason": "runtime_import_or_file_error"}),
                    "source": "runtime",
                    "status": "open",
                }
            )
        return rows

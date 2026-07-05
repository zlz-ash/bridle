"""Debounced file-change watcher → incremental code-map refresh."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from bridle.features.project_map.store import ProjectPlanStore

logger = logging.getLogger("bridle.map_watcher")

_WATCH_INTERVAL_SECONDS = 2.0
_DEBOUNCE_SECONDS = 1.0
_SOURCE_SUFFIXES = (".py", ".ts", ".tsx")


class CodeMapRefreshWatcher:
    """Poll workspace mtimes and call refresh_code_paths for changed source files."""

    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._stop: dict[str, threading.Event] = {}
        self._mtimes: dict[str, dict[str, float]] = {}

    def start(self, project_root: Path, *, project_id: str) -> None:
        """Start watching one project root; idempotent per project_id."""
        key = project_id
        if key in self._threads and self._threads[key].is_alive():
            return
        stop_event = threading.Event()
        self._stop[key] = stop_event
        root = Path(project_root).resolve()
        self._mtimes[key] = self._snapshot(root)
        thread = threading.Thread(
            target=self._run,
            args=(root, key, stop_event),
            name=f"map-watcher-{project_id[:8]}",
            daemon=True,
        )
        self._threads[key] = thread
        thread.start()
        logger.info("map_watcher_started project_id=%s root=%s", project_id, root)

    def stop(self, project_id: str) -> None:
        """Stop watcher for one project."""
        event = self._stop.pop(project_id, None)
        if event is not None:
            event.set()

    def _run(self, root: Path, project_id: str, stop_event: threading.Event) -> None:
        pending: set[str] = set()
        last_change = 0.0
        store = ProjectPlanStore(root, project_id=project_id)
        while not stop_event.is_set():
            try:
                current = self._snapshot(root)
                previous = self._mtimes.get(project_id, {})
                deleted = sorted(set(previous.keys()) - set(current.keys()))
                changed = [
                    rel
                    for rel, mtime in current.items()
                    if previous.get(rel) != mtime
                ]
                self._mtimes[project_id] = current
                if changed:
                    pending.update(changed)
                    last_change = time.monotonic()
                if deleted:
                    pending.update(deleted)
                    last_change = time.monotonic()
                if pending and (time.monotonic() - last_change) >= _DEBOUNCE_SECONDS:
                    batch = sorted(pending)
                    pending.clear()
                    if store.database_path.is_file():
                        store.refresh_code_paths(batch)
                        logger.info(
                            "map_watcher_refreshed project_id=%s paths=%s",
                            project_id,
                            batch,
                        )
            except Exception:
                logger.exception("map_watcher_error project_id=%s", project_id)
            stop_event.wait(_WATCH_INTERVAL_SECONDS)

    @staticmethod
    def _snapshot(root: Path) -> dict[str, float]:
        mtimes: dict[str, float] = {}
        if not root.is_dir():
            return mtimes
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in _SOURCE_SUFFIXES:
                continue
            if ".bridle" in path.parts or "node_modules" in path.parts:
                continue
            try:
                rel = path.relative_to(root).as_posix()
                mtimes[rel] = path.stat().st_mtime
            except OSError:
                continue
        return mtimes


_watcher = CodeMapRefreshWatcher()


def get_code_map_watcher() -> CodeMapRefreshWatcher:
    return _watcher

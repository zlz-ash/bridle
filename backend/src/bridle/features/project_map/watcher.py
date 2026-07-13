"""Debounced file-change watcher → incremental code-map refresh."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from bridle.features.project_map.store import ProjectPlanStore

logger = logging.getLogger("bridle.map_watcher")

_WATCH_INTERVAL_SECONDS = 2.0
_DEBOUNCE_SECONDS = 1.0
_SOURCE_SUFFIXES = (".py", ".ts", ".tsx")


class CodeMapRefreshWatcher:
    """Poll workspace mtimes and call refresh_code_paths for changed source files."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._registrations: dict[str, _WatcherRegistration] = {}
        self._generation = 0

    def start(self, project_root: Path, *, project_id: str) -> None:
        """Start watching one project root; idempotent per project_id."""
        root = Path(project_root).resolve()
        with self._lock:
            current = self._registrations.get(project_id)
            if current is not None and current.thread.is_alive():
                return
            if current is not None:
                self._registrations.pop(project_id, None)
            self._generation += 1
            registration = _WatcherRegistration(
                generation=self._generation,
                stop_event=threading.Event(),
                mtimes=self._snapshot(root),
            )
            thread = threading.Thread(
                target=self._run,
                args=(root, project_id, registration),
                name=f"map-watcher-{project_id[:8]}",
                daemon=True,
            )
            registration.thread = thread
            self._registrations[project_id] = registration
            try:
                thread.start()
            except Exception:
                if self._registrations.get(project_id) is registration:
                    self._registrations.pop(project_id, None)
                logger.exception(
                    "map_watcher_start_failed project_id=%s root=%s",
                    project_id,
                    root,
                )
                raise
        logger.info("map_watcher_started project_id=%s root=%s", project_id, root)

    def stop(self, project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        """Stop and join one watcher; retain a live registration on timeout."""
        with self._lock:
            registration = self._registrations.get(project_id)
            if registration is None:
                return True
            registration.stop_event.set()
            thread = registration.thread
        thread.join(timeout=max(0.0, timeout_seconds))
        if thread.is_alive():
            logger.warning("map_watcher_stop_timeout project_id=%s", project_id)
            return False
        with self._lock:
            if self._registrations.get(project_id) is registration:
                self._registrations.pop(project_id, None)
        logger.info("map_watcher_stopped project_id=%s", project_id)
        return True

    def active_project_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    project_id
                    for project_id, registration in self._registrations.items()
                    if registration.thread.is_alive()
                )
            )

    def status(self, project_id: str) -> WatcherStatus | None:
        with self._lock:
            registration = self._registrations.get(project_id)
            if registration is None:
                return None
            return WatcherStatus(
                project_id=project_id,
                generation=registration.generation,
                thread_alive=registration.thread.is_alive(),
            )

    def _run(
        self,
        root: Path,
        project_id: str,
        registration: _WatcherRegistration,
    ) -> None:
        pending: set[str] = set()
        last_change = 0.0
        store = ProjectPlanStore(root, project_id=project_id)
        try:
            while not registration.stop_event.is_set():
                try:
                    current = self._snapshot(root)
                    previous = registration.mtimes
                    deleted = sorted(set(previous) - set(current))
                    changed = [rel for rel, mtime in current.items() if previous.get(rel) != mtime]
                    registration.mtimes = current
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
                registration.stop_event.wait(_WATCH_INTERVAL_SECONDS)
        finally:
            self._before_registration_cleanup(project_id, registration.generation)
            with self._lock:
                if self._registrations.get(project_id) is registration:
                    self._registrations.pop(project_id, None)

    def _before_registration_cleanup(self, project_id: str, generation: int) -> None:
        del project_id, generation

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


@dataclass(frozen=True)
class WatcherStatus:
    project_id: str
    generation: int
    thread_alive: bool


@dataclass
class _WatcherRegistration:
    generation: int
    stop_event: threading.Event
    mtimes: dict[str, float]
    thread: threading.Thread = field(init=False)


_watcher = CodeMapRefreshWatcher()


def get_code_map_watcher() -> CodeMapRefreshWatcher:
    return _watcher

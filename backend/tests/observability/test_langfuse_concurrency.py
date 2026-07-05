"""Concurrency tests: explicit parent chains must not cross traces."""
from __future__ import annotations

import asyncio
import threading

import pytest

from bridle.observability.config import ObservabilityConfig
from bridle.observability.context import current_active_langfuse_trace
from bridle.observability.facade import ObservabilityFacade
from bridle.observability.langfuse_adapter import LangfuseObservabilityAdapter
from bridle.observability.schema import ObservabilityContext

from .fake_langfuse import FakeLangfuse


def _config() -> ObservabilityConfig:
    return ObservabilityConfig(
        enabled=True,
        provider="langfuse",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        langfuse_host="https://langfuse.example",
    )


class TestLangfuseTraceIsolation:
    def test_active_trace_stored_in_contextvar_as_root_observation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        handle = adapter.start_trace("node_agent.run", run_id="r1")
        assert current_active_langfuse_trace() is handle._trace  # type: ignore[attr-defined]
        handle.end(status="completed")
        assert current_active_langfuse_trace() is None

    def test_concurrent_threads_keep_generation_parents_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        facade = ObservabilityFacade(_config(), adapter=adapter)
        results: dict[str, dict[str, str]] = {}
        lock = threading.Lock()

        def _worker(run_id: str) -> None:
            with facade.bind_context(ObservabilityContext(run_id=run_id)):
                handle = facade.start_trace("node_agent.run")
                facade.record_generation(
                    model="m",
                    input_summary={"run_id": run_id},
                    output_summary={"ok": True},
                    metadata={"run_id": run_id},
                )
                gen_parent = adapter._client.child_starts[-1]  # type: ignore[attr-defined]
                with lock:
                    results[run_id] = {
                        "root_id": handle._trace.id,  # type: ignore[attr-defined]
                        "gen_parent_id": gen_parent["parent_id"],
                    }
                handle.end(status="completed")

        t1 = threading.Thread(target=_worker, args=("run-a",))
        t2 = threading.Thread(target=_worker, args=("run-b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["run-a"]["gen_parent_id"] == results["run-a"]["root_id"]
        assert results["run-b"]["gen_parent_id"] == results["run-b"]["root_id"]
        assert results["run-a"]["root_id"] != results["run-b"]["root_id"]

    @pytest.mark.asyncio
    async def test_concurrent_async_tasks_do_not_mix_generations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bridle.observability.langfuse_adapter.Langfuse", FakeLangfuse)
        adapter = LangfuseObservabilityAdapter(_config())
        facade = ObservabilityFacade(_config(), adapter=adapter)
        generation_parents: dict[str, str] = {}

        async def _worker(run_id: str) -> None:
            with facade.bind_context(ObservabilityContext(run_id=run_id)):
                handle = facade.start_trace("node_agent.run")
                await asyncio.sleep(0)
                facade.record_generation(
                    model="m",
                    input_summary={"run_id": run_id},
                    output_summary={"ok": True},
                    metadata={"run_id": run_id},
                )
                gen = adapter._client.child_starts[-1]  # type: ignore[attr-defined]
                generation_parents[run_id] = gen["parent_id"]
                handle.end(status="completed")

        await asyncio.gather(_worker("run-a"), _worker("run-b"))

        roots = {
            run_id: next(
                obs.id
                for obs in adapter._client.observations  # type: ignore[attr-defined]
                if obs.name == "node_agent.run" and obs.metadata.get("run_id") == run_id
            )
            for run_id in ("run-a", "run-b")
        }
        assert generation_parents["run-a"] == roots["run-a"]
        assert generation_parents["run-b"] == roots["run-b"]
        assert roots["run-a"] != roots["run-b"]

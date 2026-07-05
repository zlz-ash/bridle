"""Shared fakes for Langfuse v4 adapter tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeObservation:
    """Minimal v4 observation stub with explicit parent chaining."""

    client: FakeLangfuse
    name: str
    as_type: str
    metadata: dict[str, Any]
    trace_id: str
    parent: FakeObservation | None = None
    id: str = field(default="")
    update_calls: list[dict[str, Any]] = field(default_factory=list)
    end_calls: list[dict[str, Any]] = field(default_factory=list)
    event_calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"obs-{self.client.next_id()}"

    def start_observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any | None = None,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> FakeObservation:
        child = FakeObservation(
            client=self.client,
            name=name,
            as_type=as_type,
            metadata=dict(metadata or {}),
            trace_id=self.trace_id,
            parent=self,
        )
        self.client.child_starts.append(
            {
                "parent_id": self.id,
                "child_id": child.id,
                "as_type": as_type,
                "name": name,
                "metadata": dict(metadata or {}),
                "model": model,
                "input": input,
                "output": output,
                **kwargs,
            }
        )
        self.client.observations.append(child)
        return child

    def update(self, **kwargs: Any) -> FakeObservation:
        self.update_calls.append(dict(kwargs))
        return self

    def end(self, **kwargs: Any) -> FakeObservation:
        self.end_calls.append(dict(kwargs))
        return self

    def create_event(self, **kwargs: Any) -> FakeObservation:
        self.event_calls.append(dict(kwargs))
        return FakeObservation(
            client=self.client,
            name=str(kwargs.get("name", "event")),
            as_type="event",
            metadata=dict(kwargs.get("metadata") or {}),
            trace_id=self.trace_id,
            parent=self,
        )


class FakeLangfuse:
    """Records root `start_observation` calls and supports static SDK probe."""

    __version__ = "4.7.1-test"

    def __init__(
        self,
        *,
        public_key: str = "",
        secret_key: str = "",
        host: str = "",
        omit_methods: frozenset[str] | None = None,
        omit_observation_methods: frozenset[str] | None = None,
        **_ignored: object,
    ) -> None:
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host
        self.omit_methods = omit_methods or frozenset()
        self.omit_observation_methods = omit_observation_methods or frozenset()
        self.root_starts: list[dict[str, Any]] = []
        self.child_starts: list[dict[str, Any]] = []
        self.observations: list[FakeObservation] = []
        self.flush_calls = 0
        self._seq = 0
        if "start_observation" in self.omit_methods:
            object.__setattr__(self, "start_observation", None)
        if "flush" in self.omit_methods:
            object.__setattr__(self, "flush", None)

    def next_id(self) -> int:
        self._seq += 1
        return self._seq

    def start_observation(
        self,
        *,
        as_type: str = "span",
        name: str,
        input: Any | None = None,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> FakeObservation:
        if getattr(self, "start_observation", None) is None:
            raise RuntimeError("start_observation unavailable on fake client")
        trace_id = f"trace-{name}-{self.next_id()}"
        obs = FakeObservation(
            client=self,
            name=name,
            as_type=as_type,
            metadata=dict(metadata or {}),
            trace_id=trace_id,
        )
        if "update" in self.omit_observation_methods:
            del obs.update  # type: ignore[attr-defined]
        if "end" in self.omit_observation_methods:
            del obs.end  # type: ignore[attr-defined]
        if "start_observation" in self.omit_observation_methods:
            del obs.start_observation  # type: ignore[attr-defined]
        if "create_event" in self.omit_observation_methods:
            del obs.create_event  # type: ignore[attr-defined]
        self.root_starts.append(
            {
                "as_type": as_type,
                "name": name,
                "metadata": dict(metadata or {}),
                "model": model,
                "input": input,
                "output": output,
                **kwargs,
            }
        )
        self.observations.append(obs)
        return obs

    def flush(self) -> None:
        self.flush_calls += 1

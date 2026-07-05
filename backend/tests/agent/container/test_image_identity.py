"""Tests for image identity resolution."""
from __future__ import annotations

from bridle.agent.container import image_identity


def test_resolve_image_identity_is_not_cached(monkeypatch) -> None:
    calls = {"count": 0}

    class Result:
        returncode = 0
        stdout = "sha256:abc111\n"

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return Result()
        result = Result()
        result.stdout = "sha256:def222\n"
        return result

    monkeypatch.setattr(image_identity.subprocess, "run", fake_run)
    first = image_identity.resolve_image_identity("bridle-agent:test")
    second = image_identity.resolve_image_identity("bridle-agent:test")
    assert first != second
    assert calls["count"] == 2

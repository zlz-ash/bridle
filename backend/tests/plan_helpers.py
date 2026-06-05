"""Shared helpers for plan import test payloads."""
from __future__ import annotations


def ensure_code_change_tests(nodes: list[dict]) -> list[dict]:
    patched: list[dict] = []
    for node in nodes:
        n = dict(node)
        if n.get("node_type") == "code_change" and not n.get("tests"):
            n["tests"] = ["pytest tests/ -q"]
        patched.append(n)
    return patched


def ensure_plan_payload(plan: dict) -> dict:
    payload = dict(plan)
    payload["nodes"] = ensure_code_change_tests(list(payload.get("nodes") or []))
    return payload

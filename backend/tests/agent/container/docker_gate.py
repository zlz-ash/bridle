"""CLI for Linux Docker CI evidence hard gates."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from .docker_evidence import (
    DockerEvidenceError,
    validate_evidence_directory_for_gate,
)

_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITHUB_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_SECRET = re.compile(
    r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*:\s*(?:bearer|basic))\s+([^\s,;]+)"
)
_DETAIL_LIMIT = 1024
_SCALAR_LIMIT = 128
_KEY_LIMIT = 32
_KEY_TEXT_LIMIT = 64
_FAILURE_JSON_LIMIT = 16_384
_TRUNCATED_TO_FIT = "[TRUNCATED_TO_FIT]"


def _bounded_text(value: object, *, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    redacted = _AUTHORIZATION.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    redacted = _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= limit:
        return redacted, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _bounded_keys(values: list[object]) -> tuple[list[str | None], int, bool]:
    bounded: list[str | None] = []
    value_truncated = False
    for value in values[:_KEY_LIMIT]:
        text, truncated = _bounded_text(value, limit=_KEY_TEXT_LIMIT)
        bounded.append(text)
        value_truncated = value_truncated or truncated
    return bounded, len(values), len(values) > _KEY_LIMIT or value_truncated


def _serialize_bounded_failure(payload: dict[str, object]) -> str:
    def serialize() -> str:
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)

    text = serialize()
    list_fields = ("entry_keys", "entry_digest_keys")
    while len(text.encode("utf-8")) > _FAILURE_JSON_LIMIT:
        populated = [field for field in list_fields if isinstance(payload.get(field), list) and payload[field]]
        if not populated:
            break
        field = max(populated, key=lambda name: len(payload[name]))
        payload[field].pop()
        payload[f"{field}_truncated"] = True
        text = serialize()

    scalar_fields = (
        "detail",
        "error_code",
        "expected_source_digest",
        "expected_image_digest",
        "expected_github_sha",
        "summary_status",
        "summary_session_id",
        "summary_source_digest",
        "summary_github_sha",
    )
    for field in scalar_fields:
        if len(text.encode("utf-8")) <= _FAILURE_JSON_LIMIT:
            break
        if payload.get(field) is None:
            continue
        payload[field] = _TRUNCATED_TO_FIT
        payload[f"{field}_truncated"] = True
        text = serialize()

    if len(text.encode("utf-8")) > _FAILURE_JSON_LIMIT:
        raise RuntimeError("docker_gate_failure_payload_exceeds_limit")
    return text


def _require_gate_identity(value: str | None, *, field: str, pattern: re.Pattern[str]) -> str:
    text = (value or "").strip()
    if not text or not pattern.fullmatch(text):
        raise DockerEvidenceError(f"docker_evidence_{field}_required", detail=text or "<empty>")
    return text


def validate_evidence_cli(
    evidence_dir: Path,
    *,
    expected_source_digest: str | None,
    expected_image_digest: str | None,
    expected_github_sha: str | None,
) -> None:
    source_digest = _require_gate_identity(expected_source_digest, field="source_digest", pattern=_SHA256_DIGEST)
    image_digest = _require_gate_identity(expected_image_digest, field="image_digest", pattern=_SHA256_DIGEST)
    github_sha = _require_gate_identity(expected_github_sha, field="github_sha", pattern=_GITHUB_SHA)
    validate_evidence_directory_for_gate(
        evidence_dir,
        expected_source_digest=source_digest,
        expected_image_digest=image_digest,
        expected_github_sha=github_sha,
    )


def _write_gate_failure(
    evidence_dir: Path,
    exc: DockerEvidenceError,
    *,
    expected_source_digest: str,
    expected_image_digest: str,
    expected_github_sha: str,
    duration_ms: int,
    exit_code: int,
) -> None:
    safe_error_code, error_code_truncated = _bounded_text(exc.error_code, limit=_SCALAR_LIMIT)
    safe_detail, detail_truncated = _bounded_text(exc.detail, limit=_DETAIL_LIMIT)
    safe_source_digest, source_digest_truncated = _bounded_text(
        expected_source_digest, limit=_SCALAR_LIMIT
    )
    safe_image_digest, image_digest_truncated = _bounded_text(
        expected_image_digest, limit=_SCALAR_LIMIT
    )
    safe_github_sha, github_sha_truncated = _bounded_text(
        expected_github_sha, limit=_SCALAR_LIMIT
    )
    payload = {
        "error_code": safe_error_code,
        "error_code_truncated": error_code_truncated,
        "detail": safe_detail,
        "detail_truncated": detail_truncated,
        "stage": "validate_docker_evidence",
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "expected_source_digest": safe_source_digest,
        "expected_source_digest_truncated": source_digest_truncated,
        "expected_image_digest": safe_image_digest,
        "expected_image_digest_truncated": image_digest_truncated,
        "expected_github_sha": safe_github_sha,
        "expected_github_sha_truncated": github_sha_truncated,
    }
    summary_path = evidence_dir / "session-summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None
        if isinstance(summary, dict):
            for field in ("status", "session_id", "source_digest", "github_sha"):
                value, truncated = _bounded_text(summary.get(field), limit=_SCALAR_LIMIT)
                payload[f"summary_{field}"] = value
                payload[f"summary_{field}_truncated"] = truncated
            entries = summary.get("entries")
            if isinstance(entries, list):
                raw_entry_keys = [
                    entry.get("test_key")
                    for entry in entries
                    if isinstance(entry, dict)
                ]
                entry_keys, entry_count, entry_keys_truncated = _bounded_keys(raw_entry_keys)
                payload["entry_keys"] = entry_keys
                payload["entry_key_count"] = entry_count
                payload["entry_keys_truncated"] = entry_keys_truncated
            digests = summary.get("entry_digests")
            if isinstance(digests, dict):
                raw_digest_keys = sorted(str(key) for key in digests)
                digest_keys, digest_count, digest_keys_truncated = _bounded_keys(raw_digest_keys)
                payload["entry_digest_keys"] = digest_keys
                payload["entry_digest_key_count"] = digest_count
                payload["entry_digest_keys_truncated"] = digest_keys_truncated
    failure_path = evidence_dir / "gate-failure.json"
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.write_text(_serialize_bounded_failure(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description="Validate Docker integration evidence artifacts.")
    parser.add_argument("evidence_dir", type=Path, help="Directory containing evidence JSON files")
    parser.add_argument("--source-digest", required=True, help="Expected normalized source digest")
    parser.add_argument("--image-digest", required=True, help="Expected verified image digest")
    parser.add_argument("--github-sha", required=True, help="Expected GitHub commit SHA")
    args = parser.parse_args(argv)

    try:
        validate_evidence_cli(
            args.evidence_dir,
            expected_source_digest=args.source_digest,
            expected_image_digest=args.image_digest,
            expected_github_sha=args.github_sha,
        )
    except DockerEvidenceError as exc:
        _write_gate_failure(
            args.evidence_dir,
            exc,
            expected_source_digest=args.source_digest,
            expected_image_digest=args.image_digest,
            expected_github_sha=args.github_sha,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            exit_code=1,
        )
        safe_error_code, _ = _bounded_text(exc.error_code, limit=_SCALAR_LIMIT)
        safe_detail, _ = _bounded_text(exc.detail, limit=_DETAIL_LIMIT)
        print(f"docker_gate_failed: {safe_error_code} {safe_detail}", file=sys.stderr)
        return 1
    print("docker_gate_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI for Linux Docker CI evidence hard gates."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .docker_evidence import (
    DockerEvidenceError,
    validate_evidence_directory_for_gate,
)

_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITHUB_SHA = re.compile(r"^[0-9a-f]{7,40}$")


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


def main(argv: list[str] | None = None) -> int:
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
        print(f"docker_gate_failed: {exc.error_code} {exc.detail}", file=sys.stderr)
        return 1
    print("docker_gate_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Resolve immutable container image identity."""
from __future__ import annotations

import hashlib
import subprocess


def resolve_image_identity(image: str = "bridle-agent:local") -> str:
    """Resolve image digest; never cache across calls so rebuilt tags refresh."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"unresolved:{hashlib.sha256(image.encode()).hexdigest()[:16]}"
    if result.returncode != 0 or not result.stdout.strip():
        return f"unresolved:{hashlib.sha256(image.encode()).hexdigest()[:16]}"
    digest = result.stdout.strip()
    if digest.startswith("sha256:"):
        return digest
    return f"sha256:{digest}"

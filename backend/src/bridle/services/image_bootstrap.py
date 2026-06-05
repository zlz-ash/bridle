"""Docker daemon check and local agent image bootstrap for ``bridle serve``."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

REQUIRED_IMAGES: tuple[str, ...] = (
    "bridle-main-agent:local",
    "bridle-node-agent:local",
)

_IMAGE_DOCKERFILES: dict[str, str] = {
    "bridle-main-agent:local": "main-agent.Dockerfile",
    "bridle-node-agent:local": "node-agent.Dockerfile",
}

_DAEMON_TIMEOUT_SEC = 10


class ImageBootstrapError(RuntimeError):
    """Bootstrap failed; ``code`` is machine-readable, message is user-facing."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ImageBootstrapService:
    """Ensure Docker daemon is up and required agent images exist locally."""

    def __init__(
        self,
        repo_root: Path,
        *,
        log: Callable[[str], None] = print,
    ) -> None:
        self.repo_root = repo_root
        self.log = log

    def ensure_ready(self, *, force_rebuild: bool = False) -> None:
        """Check daemon and build missing or forced images.

        Raises:
            ImageBootstrapError: daemon unavailable or image build failed.
        """
        self._check_daemon()
        for image in REQUIRED_IMAGES:
            if force_rebuild or not self._image_exists(image):
                self._build_image(image)

    def _check_daemon(self) -> None:
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DAEMON_TIMEOUT_SEC,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ImageBootstrapError(
                "docker_daemon_unavailable",
                "请先启动 Docker Desktop，等托盘鲸鱼变绿后重试。",
            ) from exc
        if result.returncode != 0:
            raise ImageBootstrapError(
                "docker_daemon_unavailable",
                "请先启动 Docker Desktop，等托盘鲸鱼变绿后重试。",
            )

    def _image_exists(self, image: str) -> bool:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DAEMON_TIMEOUT_SEC,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    def _build_image(self, image: str) -> None:
        dockerfile_name = _IMAGE_DOCKERFILES[image]
        dockerfile = self.repo_root / "docker" / dockerfile_name
        if not dockerfile.is_file():
            raise ImageBootstrapError(
                "image_build_failed",
                f"找不到 Dockerfile：{dockerfile}",
            )

        self.log(f"正在构建镜像 {image} …")
        cmd = [
            "docker",
            "build",
            "-t",
            image,
            "-f",
            str(dockerfile),
            str(self.repo_root),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        captured: list[str] = []
        for line in proc.stdout:
            stripped = line.rstrip("\n\r")
            if stripped:
                self.log(stripped)
            captured.append(line)
        returncode = proc.wait()
        if returncode != 0:
            tail = "".join(captured[-20:]).strip()
            summary = tail if tail else f"docker build 退出码 {returncode}"
            raise ImageBootstrapError("image_build_failed", summary)

"""Workspace file read API tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_read_text_file_preserves_crlf(client: AsyncClient, test_workspace: Path) -> None:
    target = test_workspace / "src" / "crlf.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"print('hi')\r\n")

    response = await client.get("/api/v1/workspace/files", params={"path": "src/crlf.py"})
    assert response.status_code == 200
    assert response.json()["content"] == "print('hi')\r\n"


@pytest.mark.asyncio
async def test_read_text_file(client: AsyncClient, test_workspace: Path) -> None:
    target = test_workspace / "src" / "x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('hi')\n", encoding="utf-8")

    response = await client.get("/api/v1/workspace/files", params={"path": "src/x.py"})
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "src/x.py"
    assert body["content"] == target.read_bytes().decode("utf-8")
    assert body["encoding"] == "utf-8"
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_path_traversal_forbidden(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspace/files", params={"path": "../etc/passwd"})
    assert response.status_code == 403
    assert response.json()["code"] == "path_outside_workspace"


@pytest.mark.asyncio
async def test_git_path_denied(client: AsyncClient, test_workspace: Path) -> None:
    git_head = test_workspace / ".git" / "HEAD"
    git_head.parent.mkdir(parents=True, exist_ok=True)
    git_head.write_text("ref: refs/heads/main\n", encoding="utf-8")

    response = await client.get("/api/v1/workspace/files", params={"path": ".git/HEAD"})
    assert response.status_code == 403
    assert response.json()["code"] == "path_denied"


@pytest.mark.asyncio
async def test_missing_file_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspace/files", params={"path": "missing.txt"})
    assert response.status_code == 404
    assert response.json()["code"] == "file_not_found"


@pytest.mark.asyncio
async def test_directory_returns_400(client: AsyncClient, test_workspace: Path) -> None:
    directory = test_workspace / "src"
    directory.mkdir(parents=True, exist_ok=True)
    response = await client.get("/api/v1/workspace/files", params={"path": "src"})
    assert response.status_code == 400
    assert response.json()["details"]["reason"] == "is_directory"


@pytest.mark.asyncio
async def test_large_file_413(client: AsyncClient, test_workspace: Path) -> None:
    target = test_workspace / "big.txt"
    target.write_bytes(b"x" * (1_100_000))
    response = await client.get("/api/v1/workspace/files", params={"path": "big.txt"})
    assert response.status_code == 413
    assert response.json()["details"]["size"] == 1_100_000


@pytest.mark.asyncio
async def test_binary_file_415(client: AsyncClient, test_workspace: Path) -> None:
    target = test_workspace / "bin.dat"
    target.write_bytes(b"\x00binary")
    response = await client.get("/api/v1/workspace/files", params={"path": "bin.dat"})
    assert response.status_code == 415
    assert response.json()["details"]["reason"] == "binary"
    assert "content" not in response.json()


@pytest.mark.asyncio
async def test_utf8_bom_stripped(client: AsyncClient, test_workspace: Path) -> None:
    target = test_workspace / "bom.txt"
    target.write_bytes("\ufeffhello".encode("utf-8"))
    response = await client.get("/api/v1/workspace/files", params={"path": "bom.txt"})
    assert response.status_code == 200
    assert response.json()["content"] == "hello"


@pytest.mark.asyncio
async def test_gbk_fallback_replace(client: AsyncClient, test_workspace: Path) -> None:
    target = test_workspace / "gbk.txt"
    target.write_bytes("中文".encode("gbk"))
    response = await client.get("/api/v1/workspace/files", params={"path": "gbk.txt"})
    assert response.status_code == 200
    assert response.json()["encoding"] == "utf-8-fallback-replace"

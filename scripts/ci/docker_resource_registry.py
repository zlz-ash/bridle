#!/usr/bin/env python3
"""Run-scoped Docker container and image tag ownership registry."""
from __future__ import annotations

import logging
from dataclasses import dataclass

LOGGER = logging.getLogger("bridle.docker_resource_registry")


@dataclass(frozen=True)
class ContainerOwnership:
    run_id: str
    name: str
    container_id: str


@dataclass(frozen=True)
class TagOwnership:
    run_id: str
    tag: str
    image_id: str


class DockerResourceRegistry:
    def __init__(self) -> None:
        self._containers: dict[str, ContainerOwnership] = {}
        self._tags: dict[str, TagOwnership] = {}

    def register_container(self, *, run_id: str, name: str, container_id: str) -> None:
        existing = self._containers.get(name)
        if existing is not None and existing.run_id != run_id:
            raise RuntimeError(f"foreign_container_name_rebind name={name}")
        self._containers[name] = ContainerOwnership(run_id=run_id, name=name, container_id=container_id)
        LOGGER.info("docker_registry_container_registered run_id=%s name=%s id=%s", run_id, name, container_id)

    def verify_container(self, *, run_id: str, name: str, container_id: str) -> None:
        record = self._containers.get(name)
        if record is None:
            raise RuntimeError(f"container_not_registered name={name}")
        if record.run_id != run_id:
            raise RuntimeError(f"foreign_container_owner name={name}")
        if record.container_id != container_id:
            raise RuntimeError(f"container_identity_mismatch name={name}")

    def release_container(self, *, run_id: str, name: str) -> None:
        record = self._containers.get(name)
        if record is None:
            return
        if record.run_id != run_id:
            raise RuntimeError(f"foreign_container_release name={name}")
        del self._containers[name]

    def register_tag(self, *, run_id: str, tag: str, image_id: str) -> None:
        existing = self._tags.get(tag)
        if existing is not None and existing.run_id != run_id:
            raise RuntimeError(f"foreign_tag_rebind tag={tag}")
        self._tags[tag] = TagOwnership(run_id=run_id, tag=tag, image_id=image_id)
        LOGGER.info("docker_registry_tag_registered run_id=%s tag=%s id=%s", run_id, tag, image_id)

    def verify_tag(self, *, run_id: str, tag: str, image_id: str) -> None:
        record = self._tags.get(tag)
        if record is None:
            raise RuntimeError(f"tag_not_registered tag={tag}")
        if record.run_id != run_id:
            raise RuntimeError(f"foreign_tag_owner tag={tag}")
        if record.image_id != image_id:
            raise RuntimeError(f"tag_identity_mismatch tag={tag}")

    def release_tag(self, *, run_id: str, tag: str) -> None:
        record = self._tags.get(tag)
        if record is None:
            return
        if record.run_id != run_id:
            raise RuntimeError(f"foreign_tag_release tag={tag}")
        del self._tags[tag]

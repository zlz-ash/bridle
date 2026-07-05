"""Parametrized container identity validation tests."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bridle.agent.container.container_identity import validate_container_identity
from bridle.agent.container.runner import ContainerMount, ContainerRequest

_KEEP_ALIVE = ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"]


def _base_request(test_workspace: Path) -> ContainerRequest:
    mount_a = ContainerMount(
        source=test_workspace / "slot" / "project",
        target="/workspace/project",
        readonly=False,
    )
    mount_b = ContainerMount(
        source=test_workspace / "slot" / "baseline",
        target="/workspace/baseline",
        readonly=True,
    )
    labels = {
        "bridle.schema": "v1",
        "bridle.project": "proj",
        "bridle.module": "mod",
        "bridle.boundary_fp": "fp-1",
        "bridle.image_version": "sha256:abc",
        "bridle.mount_id": "mount1",
    }
    return ContainerRequest(
        name="n1",
        image="bridle-agent:review",
        image_id="sha256:deadbeef",
        run_user="1000",
        network_mode="none",
        mounts=[mount_a, mount_b],
        labels=labels,
        command=_KEEP_ALIVE,
        module_id="mod",
        boundary_fingerprint="fp-1",
        image_version="sha256:abc",
        keep_alive=True,
        read_only_root=True,
        cap_drop=("ALL",),
        security_opt=("no-new-privileges",),
        pids_limit=256,
        memory="512m",
        cpus="1.0",
    )


class TestValidateContainerIdentity:
    def test_mount_order_independent(self, test_workspace: Path) -> None:
        expected = _base_request(test_workspace)
        actual = ContainerRequest(
            name=expected.name,
            image=expected.image,
            image_id=expected.image_id,
            run_user=expected.run_user,
            network_mode=expected.network_mode,
            mounts=list(reversed(expected.mounts)),
            labels=dict(expected.labels),
            command=list(expected.command),
            module_id=expected.module_id,
            boundary_fingerprint=expected.boundary_fingerprint,
            image_version=expected.image_version,
            keep_alive=True,
            read_only_root=True,
            cap_drop=expected.cap_drop,
            security_opt=expected.security_opt,
            pids_limit=expected.pids_limit,
            memory=expected.memory,
            cpus=expected.cpus,
        )
        assert validate_container_identity(expected, actual) == []

    @pytest.mark.parametrize(
        ("mutator", "expected_error"),
        [
            (lambda r: replace(r, image_id="sha256:other"), "image_id_mismatch"),
            (lambda r: replace(r, run_user="0"), "run_user_mismatch"),
            (lambda r: replace(r, read_only_root=False), "read_only_root_mismatch"),
            (lambda r: replace(r, command=["sleep", "9999"]), "command_mismatch"),
            (lambda r: replace(r, network_mode="bridge"), "network_mode_mismatch"),
            (lambda r: replace(r, name="other-name"), "name_mismatch"),
            (lambda r: replace(r, privileged=True), "privileged_mismatch"),
            (lambda r: replace(r, cap_drop=()), "cap_drop_mismatch"),
            (lambda r: replace(r, security_opt=()), "security_opt_mismatch"),
            (lambda r: replace(r, pids_limit=0), "pids_limit_mismatch"),
            (lambda r: replace(r, memory="256m"), "memory_mismatch"),
            (lambda r: replace(r, cpus="2.0"), "cpus_mismatch"),
        ],
    )
    def test_rejects_identity_field_mismatch(
        self,
        test_workspace: Path,
        mutator,
        expected_error: str,
    ) -> None:
        expected = _base_request(test_workspace)
        actual = mutator(expected)
        errors = validate_container_identity(expected, actual)
        assert expected_error in errors

    def test_rejects_duplicate_mount_targets(self, test_workspace: Path) -> None:
        expected = _base_request(test_workspace)
        dup = ContainerMount(source=expected.mounts[0].source, target="/workspace/project", readonly=False)
        actual = replace(expected, mounts=[expected.mounts[0], dup])
        assert "duplicate_mount" in validate_container_identity(expected, actual)

    def test_rejects_missing_image_id_on_inspect(self, test_workspace: Path) -> None:
        expected = _base_request(test_workspace)
        actual = replace(expected, image_id="")
        assert "image_id_missing" in validate_container_identity(expected, actual)

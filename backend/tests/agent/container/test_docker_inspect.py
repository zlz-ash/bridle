"""Tests for Docker inspect → ContainerRequest hardening field parsing."""
from __future__ import annotations

from bridle.agent.container.docker_inspect import request_from_inspect_data


def _inspect_payload(**host_overrides) -> dict:
    host_config = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "PidsLimit": 256,
        "Memory": 536870912,
        "NanoCpus": 1000000000,
    }
    host_config.update(host_overrides)
    return {
        "Id": "abc123",
        "Name": "/bridle-mod-fp",
        "Image": "sha256:deadbeef",
        "Config": {
            "Image": "bridle-agent:review",
            "User": "1000",
            "Cmd": ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"],
            "Labels": {
                "bridle.schema": "v1",
                "bridle.project": "proj",
                "bridle.module": "mod",
                "bridle.boundary_fp": "fp-1",
                "bridle.image_version": "sha256:abc",
                "bridle.mount_id": "mount1",
            },
        },
        "HostConfig": host_config,
        "Mounts": [],
    }


class TestDockerInspectHardening:
    def test_parses_hardening_fields(self) -> None:
        request = request_from_inspect_data(_inspect_payload())
        assert request is not None
        assert request.privileged is False
        assert request.cap_drop == ("ALL",)
        assert request.security_opt == ("no-new-privileges",)
        assert request.pids_limit == 256
        assert request.memory == "512m"
        assert request.cpus == "1.0"
        assert request.run_user == "1000"
        assert request.read_only_root is True

    def test_parses_privileged_container(self) -> None:
        request = request_from_inspect_data(_inspect_payload(Privileged=True, CapDrop=[]))
        assert request is not None
        assert request.privileged is True
        assert request.cap_drop == ()

    def test_parses_memory_and_cpu_variants(self) -> None:
        request = request_from_inspect_data(
            _inspect_payload(Memory=1073741824, NanoCpus=500000000)
        )
        assert request is not None
        assert request.memory == "1g"
        assert request.cpus == "0.5"

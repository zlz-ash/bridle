"""Unit tests for identity-verified Docker test image cleanup."""
from __future__ import annotations

import json
import subprocess

import pytest

from . import docker_test_resources as dtr

OWNED_ID = "sha256:" + "a" * 64
FOREIGN_ID = "sha256:" + "b" * 64
THIRD_ID = "sha256:" + "c" * 64


class _FakeDockerState:
    """Minimal Docker state shared by inspect/list/rmi handlers in adapter tests."""

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        self.tag_to_image: dict[str, str] = {}
        self.present_images: set[str] = set()
        self.labels: dict[str, dict[str, str]] = {}
        self.commands: list[list[str]] = []
        self.tag_rmi_failures_remaining: dict[str, int] = {}

    def add_image(self, image_id: str, *, tags: list[str], owner_run_id: str | None = None) -> None:
        owner = owner_run_id or self.run_id
        self.present_images.add(image_id)
        self.labels[image_id] = {dtr.IT_LABEL: owner}
        for tag in tags:
            self.tag_to_image[tag] = image_id

    def tags_for_image(self, image_id: str) -> set[str]:
        return {tag for tag, resolved in self.tag_to_image.items() if resolved == image_id}

    def resolve_ref(self, ref: str) -> str | None:
        if ref in self.tag_to_image:
            return self.tag_to_image[ref]
        if ref in self.present_images:
            return ref
        return None

    def list_images_for_label(self, owner_run_id: str) -> list[str]:
        ids = [
            image_id
            for image_id in sorted(self.present_images)
            if self.labels.get(image_id, {}).get(dtr.IT_LABEL) == owner_run_id
        ]
        return ids

    def remove_tag(self, tag: str) -> None:
        image_id = self.tag_to_image.pop(tag, None)
        if image_id is not None and not self.tags_for_image(image_id):
            self.present_images.discard(image_id)
            self.labels.pop(image_id, None)

    def remove_image_id(self, image_id: str) -> bool:
        if self.tags_for_image(image_id):
            return False
        self.present_images.discard(image_id)
        self.labels.pop(image_id, None)
        return True

    def bind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dtr, "_run_docker", self.run)

    def run(self, args: list[str], timeout: int = 15) -> dtr.DockerTransportResult:
        del timeout
        self.commands.append(list(args))
        if args[:3] == ["image", "inspect", "-f"] and args[3].startswith("{{.Id}}"):
            ref = args[4]
            image_id = self.resolve_ref(ref)
            if image_id is None:
                return _transport("exited", returncode=1, stderr="No such image")
            return _transport("exited", returncode=0, stdout=image_id)
        if args[:3] == ["image", "inspect", "-f"] and "Labels" in args[3]:
            ref = args[4]
            image_id = self.resolve_ref(ref)
            if image_id is None or image_id not in self.labels:
                return _transport("exited", returncode=1, stderr="No such image")
            return _transport("exited", returncode=0, stdout=json.dumps(self.labels[image_id]))
        if args[:2] == ["image", "inspect"] and len(args) == 3:
            ref = args[2]
            if self.resolve_ref(ref) is None:
                return _transport("exited", returncode=1, stderr="No such image")
            return _transport("exited", returncode=0)
        if args[:1] == ["images"] and "--no-trunc" in args and "-q" in args:
            filter_idx = args.index("--filter")
            filter_val = args[filter_idx + 1]
            prefix = f"label={dtr.IT_LABEL}="
            if not filter_val.startswith(prefix):
                return _transport("exited", returncode=1, stderr="unsupported filter")
            owner = filter_val[len(prefix) :]
            listed = self.list_images_for_label(owner)
            stdout = "\n".join(listed)
            if stdout:
                stdout += "\n"
            return _transport("exited", returncode=0, stdout=stdout)
        if args[:2] == ["ps", "-aq"]:
            return _transport("exited", returncode=0, stdout="")
        if args[:1] == ["rmi"] and len(args) == 2:
            target = args[1]
            if target in self.tag_to_image:
                remaining = self.tag_rmi_failures_remaining.get(target, 0)
                if remaining > 0:
                    self.tag_rmi_failures_remaining[target] = remaining - 1
                    return _transport("exited", returncode=1, stderr="untag denied")
                self.remove_tag(target)
                return _transport("exited", returncode=0)
            if target.startswith("sha256:"):
                if not self.remove_image_id(target):
                    return _transport("exited", returncode=1, stderr="conflict: tag referenced")
                return _transport("exited", returncode=0)
        return _transport("exited", returncode=1, stderr=f"unexpected: {args}")


def _reset_registry() -> None:
    dtr._REGISTRY.clear()
    dtr._TAG_REGISTRY.clear()


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    _reset_registry()
    yield
    _reset_registry()


def test_parse_image_id_accepts_full_and_bare_hex() -> None:
    bare = "d" * 64
    assert dtr.parse_image_id(f"sha256:{bare}") == f"sha256:{bare}"
    assert dtr.parse_image_id(bare) == f"sha256:{bare}"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "sha256:abc",
        "sha256:" + "g" * 64,
        "not-an-id",
    ],
)
def test_parse_image_id_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        dtr.parse_image_id(raw)


def test_cleanup_removes_when_tag_and_label_match(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = dtr.RegisteredImage(
        tag="bridle-agent:test-owned",
        image_id=OWNED_ID,
        owner_run_id="run-abc",
    )
    dtr._REGISTRY["run-abc"] = [reg]

    monkeypatch.setattr(dtr, "_image_presence", lambda _image_id: ("present", None))
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: reg.owner_run_id}, None))
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=reg.image_id),
    )
    monkeypatch.setattr(
        dtr,
        "_run_docker",
        lambda args, timeout=15: (
            _transport("exited", returncode=0)
            if args[:2] == ["rmi", reg.image_id]
            else _transport("exited", returncode=1)
        ),
    )

    result = dtr.cleanup_registered_image(reg)
    assert result.removed is True
    assert result.status == "removed"
    assert "run-abc" not in dtr._REGISTRY


def test_cleanup_refuses_when_tag_rebound(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = dtr.RegisteredImage(
        tag="bridle-agent:test-owned",
        image_id=OWNED_ID,
        owner_run_id="run-abc",
    )
    dtr._REGISTRY["run-abc"] = [reg]
    removed: list[str] = []

    def fake_run(args, timeout=15):
        del timeout
        if args[:2] == ["rmi", reg.image_id]:
            removed.append(reg.image_id)
        return _transport("exited", returncode=0)

    monkeypatch.setattr(dtr, "_image_presence", lambda _image_id: ("present", None))
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: reg.owner_run_id}, None))
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=FOREIGN_ID),
    )
    monkeypatch.setattr(dtr, "_run_docker", fake_run)

    result = dtr.cleanup_registered_image(reg)
    assert result.removed is False
    assert result.status == "refused"
    assert result.error_code == dtr.IMAGE_IDENTITY_MISMATCH
    assert removed == []
    assert dtr._REGISTRY["run-abc"] == [reg]


def test_cleanup_query_failure_is_not_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = dtr.RegisteredImage(
        tag="bridle-agent:test-owned",
        image_id=OWNED_ID,
        owner_run_id="run-abc",
    )
    dtr._REGISTRY["run-abc"] = [reg]

    monkeypatch.setattr(dtr, "_image_presence", lambda _image_id: ("present", None))
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: reg.owner_run_id}, None))
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(
            status="query_failed",
            error_code=dtr.IMAGE_QUERY_FAILED,
            detail="daemon unavailable",
            transport=_transport("failed_before_exec", error="daemon unavailable"),
        ),
    )

    result = dtr.cleanup_registered_image(reg)
    assert result.status == "query_failed"
    assert result.error_code == dtr.IMAGE_QUERY_FAILED
    assert result.error_code != dtr.IMAGE_IDENTITY_MISMATCH
    assert dtr._REGISTRY["run-abc"] == [reg]


def test_cleanup_keeps_registry_on_remove_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = dtr.RegisteredImage(
        tag="bridle-agent:test-owned",
        image_id=OWNED_ID,
        owner_run_id="run-abc",
    )
    dtr._REGISTRY["run-abc"] = [reg]

    monkeypatch.setattr(dtr, "_image_presence", lambda _image_id: ("present", None))
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: reg.owner_run_id}, None))
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=reg.image_id),
    )
    monkeypatch.setattr(
        dtr,
        "_run_docker",
        lambda args, timeout=15: _transport("exited", returncode=1, stderr="remove denied"),
    )

    result = dtr.cleanup_registered_image(reg)
    assert result.removed is False
    assert result.status == "failed"
    assert result.error_code == dtr.IMAGE_REMOVE_FAILED
    assert result.command_stderr == "remove denied"
    assert dtr._REGISTRY["run-abc"] == [reg]


def test_cleanup_rejects_invalid_registered_identity() -> None:
    reg = dtr.RegisteredImage(tag="t", image_id="abc", owner_run_id="run-x")
    result = dtr.cleanup_registered_image(reg)
    assert result.status == "invalid_identity"
    assert result.error_code == dtr.IMAGE_ID_INVALID


def test_run_docker_failed_before_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        del args, kwargs
        raise OSError("cannot exec docker")

    monkeypatch.setattr(dtr.subprocess, "run", boom)
    transport = dtr._run_docker(["version"])
    assert transport.phase == "failed_before_exec"
    assert "cannot exec docker" in transport.error


def test_run_docker_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(dtr.subprocess, "run", slow)
    transport = dtr._run_docker(["version"], timeout=1)
    assert transport.phase == "timed_out"
    assert transport.timed_out is True


def test_query_image_identity_distinguishes_absent_and_query_failed() -> None:
    absent = dtr.ImageIdentityQuery(status="absent")
    failed = dtr.ImageIdentityQuery(status="query_failed", error_code=dtr.IMAGE_QUERY_FAILED)
    assert absent.status != failed.status


def test_cleanup_retry_after_inspect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = dtr.RegisteredImage(tag="t", image_id=OWNED_ID, owner_run_id="run-x")
    dtr._REGISTRY["run-x"] = [reg]
    calls = {"presence": 0}

    def presence(_image_id: str):
        calls["presence"] += 1
        if calls["presence"] == 1:
            return "unknown", _transport("failed_before_exec", error="temporary")
        return "present", None

    monkeypatch.setattr(dtr, "_image_presence", presence)
    first = dtr.cleanup_registered_image(reg)
    assert first.status == "query_failed"
    assert dtr._REGISTRY["run-x"] == [reg]

    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: "run-x"}, None))
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=OWNED_ID),
    )
    monkeypatch.setattr(
        dtr,
        "_run_docker",
        lambda args, timeout=15: _transport("exited", returncode=0),
    )
    second = dtr.cleanup_registered_image(reg)
    assert second.removed is True
    assert "run-x" not in dtr._REGISTRY


def test_list_images_for_run_rejects_short_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dtr,
        "_run_docker",
        lambda args, timeout=15: _transport("exited", returncode=0, stdout="abc\n"),
    )
    image_ids, error = dtr.list_images_for_run("run-x")
    assert image_ids == []
    assert error is not None
    assert error.returncode == 1


def test_list_images_for_run_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    duplicate = f"{OWNED_ID}\n{OWNED_ID}\n"
    monkeypatch.setattr(
        dtr,
        "_run_docker",
        lambda args, timeout=15: _transport("exited", returncode=0, stdout=duplicate),
    )
    image_ids, error = dtr.list_images_for_run("run-x")
    assert error is None
    assert image_ids == [OWNED_ID]


def test_cleanup_images_for_run_keeps_registry_on_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = dtr.RegisteredImage(tag="t", image_id=OWNED_ID, owner_run_id="run-x")
    dtr._REGISTRY["run-x"] = [reg]
    refused = dtr.ImageCleanupResult(
        tag="t",
        image_id=OWNED_ID,
        owner_run_id="run-x",
        removed=False,
        status="refused",
        error_code=dtr.IMAGE_IDENTITY_MISMATCH,
    )

    monkeypatch.setattr(dtr, "cleanup_tag_aliases_for_run", lambda _run_id: [])
    monkeypatch.setattr(dtr, "cleanup_registered_image", lambda _reg: refused)
    monkeypatch.setattr(dtr, "list_images_for_run", lambda _run_id: ([], None))

    results = dtr.cleanup_registered_images_for_run("run-x")
    assert results == [refused]
    assert dtr._REGISTRY["run-x"] == [reg]


def test_cleanup_tag_alias_untags_by_name_not_image_id(monkeypatch: pytest.MonkeyPatch) -> None:
    alias = dtr.RegisteredTag(
        tag="bridle-agent:shared",
        owner_run_id="run-x",
        registered_image_id=FOREIGN_ID,
    )
    dtr._TAG_REGISTRY["run-x"] = [alias]
    commands: list[list[str]] = []

    def fake_run(args, timeout=15):
        del timeout
        commands.append(args)
        if args[:2] == ["rmi", alias.tag]:
            return _transport("exited", returncode=0)
        return _transport("exited", returncode=1)

    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda ref: dtr.ImageIdentityQuery(status="resolved", image_id=FOREIGN_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: "run-x"}, None))
    monkeypatch.setattr(dtr, "_run_docker", fake_run)

    result = dtr.cleanup_tag_alias(alias)
    assert result.removed is True
    assert result.status == "untagged"
    assert commands[0] == ["rmi", alias.tag]
    assert not any(cmd[:2] == ["rmi", FOREIGN_ID] for cmd in commands)


def test_cleanup_tag_alias_refuses_foreign_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    alias = dtr.RegisteredTag(
        tag="bridle-agent:foreign",
        owner_run_id="run-x",
        registered_image_id=FOREIGN_ID,
    )

    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _ref: dtr.ImageIdentityQuery(status="resolved", image_id=FOREIGN_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: "other-run"}, None))

    result = dtr.cleanup_tag_alias(alias)
    assert result.status == "refused"
    assert result.error_code == dtr.TAG_FOREIGN_OWNER


def test_finalize_rebind_scenario_clears_aliases_then_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real cleanup helpers; fake Docker state drives inspect/list/rmi."""
    owned_tag = "bridle-agent:owned"
    decoy_tag = "bridle-agent:decoy"
    run_id = "run-x"
    dtr._REGISTRY[run_id] = [
        dtr.RegisteredImage(tag=owned_tag, image_id=OWNED_ID, owner_run_id=run_id),
        dtr.RegisteredImage(tag=decoy_tag, image_id=FOREIGN_ID, owner_run_id=run_id),
    ]
    dtr._TAG_REGISTRY[run_id] = [
        dtr.RegisteredTag(tag=owned_tag, owner_run_id=run_id, registered_image_id=OWNED_ID),
        dtr.RegisteredTag(tag=decoy_tag, owner_run_id=run_id, registered_image_id=FOREIGN_ID),
    ]
    state = _FakeDockerState(run_id=run_id)
    state.add_image(OWNED_ID, tags=[])
    state.add_image(FOREIGN_ID, tags=[decoy_tag, owned_tag])
    state.bind(monkeypatch)

    result = dtr.finalize_run_teardown(run_id)
    dtr.assert_run_teardown_clean(result)
    assert run_id not in dtr._REGISTRY
    assert run_id not in dtr._TAG_REGISTRY
    assert state.present_images == set()
    assert state.tag_to_image == {}
    rmi_targets = [cmd[1] for cmd in state.commands if len(cmd) >= 2 and cmd[0] == "rmi"]
    assert owned_tag in rmi_targets
    assert decoy_tag in rmi_targets
    assert rmi_targets.index(owned_tag) < rmi_targets.index(decoy_tag)
    assert OWNED_ID in rmi_targets
    assert FOREIGN_ID not in rmi_targets


def test_untag_failure_keeps_tag_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    alias = dtr.RegisteredTag(tag="bridle-agent:owned", owner_run_id="run-x", registered_image_id=FOREIGN_ID)
    dtr._TAG_REGISTRY["run-x"] = [alias]

    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _ref: dtr.ImageIdentityQuery(status="resolved", image_id=FOREIGN_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: "run-x"}, None))
    monkeypatch.setattr(
        dtr,
        "_run_docker",
        lambda args, timeout=15: _transport("exited", returncode=1, stderr="untag denied"),
    )

    result = dtr.cleanup_tag_alias(alias)
    assert result.status == "failed"
    assert dtr._TAG_REGISTRY["run-x"] == [alias]

    teardown = dtr.RunTeardownResult(
        owner_run_id="run-x",
        tag_results=[result],
        image_results=[],
        container_results=[],
        remaining_container_count=0,
        remaining_image_count=0,
        remaining_image_registry_count=0,
        remaining_tag_registry_count=1,
        query_failures=[],
    )
    with pytest.raises(AssertionError, match="tag registry"):
        dtr.assert_run_teardown_clean(teardown)


def test_untag_failure_then_retry_clears_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    owned_tag = "bridle-agent:owned"
    run_id = "run-x"
    alias = dtr.RegisteredTag(tag=owned_tag, owner_run_id=run_id, registered_image_id=FOREIGN_ID)
    reg = dtr.RegisteredImage(tag=owned_tag, image_id=OWNED_ID, owner_run_id=run_id)
    dtr._TAG_REGISTRY[run_id] = [alias]
    dtr._REGISTRY[run_id] = [reg]

    state = _FakeDockerState(run_id=run_id)
    state.add_image(OWNED_ID, tags=[])
    state.add_image(FOREIGN_ID, tags=[owned_tag])
    state.tag_rmi_failures_remaining[owned_tag] = 1
    state.bind(monkeypatch)

    first = dtr.finalize_run_teardown(run_id)
    assert any(item.status == "failed" for item in first.tag_results)
    assert run_id in dtr._TAG_REGISTRY
    assert run_id in dtr._REGISTRY
    assert set(state.list_images_for_label(run_id)) == {FOREIGN_ID, OWNED_ID}
    with pytest.raises(AssertionError):
        dtr.assert_run_teardown_clean(first)

    second = dtr.finalize_run_teardown(run_id)
    dtr.assert_run_teardown_clean(second)
    assert run_id not in dtr._TAG_REGISTRY
    assert run_id not in dtr._REGISTRY
    assert state.present_images == set()
    assert state.tag_to_image == {}
    assert state.list_images_for_label(run_id) == []

    rmi_targets = [cmd[1] for cmd in state.commands if len(cmd) >= 2 and cmd[0] == "rmi"]
    assert rmi_targets.count(owned_tag) == 2
    assert rmi_targets.index(owned_tag) < rmi_targets.index(OWNED_ID)
    # Fallback may attempt FOREIGN removal while tag still holds; untag must succeed first.
    assert rmi_targets.index(owned_tag) < rmi_targets.index(FOREIGN_ID)


def test_finalize_run_teardown_continues_after_primary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = dtr.ContainerCleanupResult(
        container_id="c1",
        owner_run_id="run-x",
        removed=False,
        status="failed",
        error_code=dtr.CONTAINER_REMOVE_FAILED,
    )
    monkeypatch.setattr(dtr, "cleanup_containers_for_run", lambda _run_id: [failed])
    monkeypatch.setattr(dtr, "cleanup_tag_aliases_for_run", lambda _run_id: [])
    monkeypatch.setattr(dtr, "cleanup_registered_images_for_run", lambda _run_id: [])
    monkeypatch.setattr(dtr, "list_containers_for_run", lambda _run_id: ([], None))
    monkeypatch.setattr(dtr, "list_images_for_run", lambda _run_id: ([], None))

    result = dtr.finalize_run_teardown("run-x")
    assert result.container_results == [failed]
    assert result.tag_results == []
    assert result.remaining_container_count == 0
    assert result.remaining_image_count == 0


def test_assert_run_teardown_clean_fails_on_image_refused() -> None:
    result = dtr.RunTeardownResult(
        owner_run_id="run-x",
        tag_results=[],
        image_results=[
            dtr.ImageCleanupResult(
                tag="t",
                image_id=OWNED_ID,
                owner_run_id="run-x",
                removed=False,
                status="refused",
                error_code=dtr.IMAGE_IDENTITY_MISMATCH,
            )
        ],
        container_results=[],
        remaining_container_count=0,
        remaining_image_count=0,
        remaining_image_registry_count=0,
        remaining_tag_registry_count=0,
        query_failures=[],
    )
    with pytest.raises(AssertionError, match="status=refused"):
        dtr.assert_run_teardown_clean(result)


def test_assert_run_teardown_clean_fails_on_image_registry_residual() -> None:
    result = dtr.RunTeardownResult(
        owner_run_id="run-x",
        tag_results=[],
        image_results=[],
        container_results=[],
        remaining_container_count=0,
        remaining_image_count=0,
        remaining_image_registry_count=1,
        remaining_tag_registry_count=0,
        query_failures=[],
    )
    with pytest.raises(AssertionError, match="image registry"):
        dtr.assert_run_teardown_clean(result)


def test_assert_run_teardown_clean_fails_on_query_error() -> None:
    result = dtr.RunTeardownResult(
        owner_run_id="run-x",
        tag_results=[],
        image_results=[],
        container_results=[],
        remaining_container_count=None,
        remaining_image_count=0,
        remaining_image_registry_count=0,
        remaining_tag_registry_count=0,
        query_failures=[dtr.CONTAINER_QUERY_FAILED],
    )
    with pytest.raises(AssertionError, match="query_failures"):
        dtr.assert_run_teardown_clean(result)


def test_register_validates_owner_on_resolved_image_id_not_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    label_targets: list[str] = []

    def inspect_labels(image_ref: str):
        label_targets.append(image_ref)
        if image_ref == OWNED_ID:
            return ({dtr.IT_LABEL: "run-1"}, None)
        if image_ref == "bridle-agent:foo":
            return ({dtr.IT_LABEL: "run-1"}, None)
        return ({dtr.IT_LABEL: "foreign"}, None)

    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=OWNED_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", inspect_labels)

    reg = dtr.register_built_image(tag="bridle-agent:foo", owner_run_id="run-1")
    assert reg.image_id == OWNED_ID
    assert label_targets == [OWNED_ID]


def test_register_fails_when_resolved_id_foreign_even_if_tag_labels_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inspect_labels(image_ref: str):
        if image_ref == OWNED_ID:
            return ({dtr.IT_LABEL: "foreign-run"}, None)
        if image_ref == "bridle-agent:foo":
            return ({dtr.IT_LABEL: "run-1"}, None)
        return ({}, None)

    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=OWNED_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", inspect_labels)

    with pytest.raises(RuntimeError, match="does not match owner_run_id"):
        dtr.register_built_image(tag="bridle-agent:foo", owner_run_id="run-1")
    assert "run-1" not in dtr._REGISTRY
    assert "run-1" not in dtr._TAG_REGISTRY


def test_register_does_not_publish_partial_state_on_label_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=OWNED_ID),
    )
    monkeypatch.setattr(
        dtr,
        "_inspect_image_labels",
        lambda _ref: (
            {},
            dtr.ImageIdentityQuery(
                status="query_failed",
                error_code=dtr.IMAGE_QUERY_FAILED,
                detail="labels unavailable",
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="failed to inspect labels"):
        dtr.register_built_image(tag="bridle-agent:foo", owner_run_id="run-1")
    assert "run-1" not in dtr._REGISTRY
    assert "run-1" not in dtr._TAG_REGISTRY


def test_register_built_image_records_under_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=OWNED_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: "run-1"}, None))

    reg = dtr.register_built_image(tag="bridle-agent:foo", owner_run_id="run-1")
    assert reg.image_id == OWNED_ID
    assert dtr._REGISTRY["run-1"] == [reg]
    assert dtr._TAG_REGISTRY["run-1"][0].tag == "bridle-agent:foo"


def test_register_rejects_label_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dtr,
        "_query_image_identity",
        lambda _tag: dtr.ImageIdentityQuery(status="resolved", image_id=OWNED_ID),
    )
    monkeypatch.setattr(dtr, "_inspect_image_labels", lambda _ref: ({dtr.IT_LABEL: "other-run"}, None))

    with pytest.raises(RuntimeError, match="does not match owner_run_id"):
        dtr.register_built_image(tag="bridle-agent:foo", owner_run_id="run-1")


def _transport(
    phase: str,
    *,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
    error: str = "",
) -> dtr.DockerTransportResult:
    return dtr.DockerTransportResult(
        phase=phase,
        command=("docker", "test"),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error=error,
    )

"""Identity-verified Docker image registration and cleanup for integration tests."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("bridle")

IT_LABEL = "bridle.it_run"
IT_TEST_IDENTITY_LABEL = "bridle.it_test_identity"
IMAGE_IDENTITY_MISMATCH = "docker_test_image_identity_mismatch"
IMAGE_REMOVE_FAILED = "docker_test_image_remove_failed"
IMAGE_QUERY_FAILED = "docker_test_query_failed"
IMAGE_ID_INVALID = "docker_test_image_id_invalid"
TAG_FOREIGN_OWNER = "docker_test_tag_foreign_owner"
TAG_UNTAG_FAILED = "docker_test_tag_untag_failed"
CONTAINER_QUERY_FAILED = "docker_test_container_query_failed"
CONTAINER_REMOVE_FAILED = "docker_test_container_remove_failed"

_FULL_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_IMAGE_ID = re.compile(r"^[0-9a-f]{64}$")

_REGISTRY: dict[str, list[RegisteredImage]] = {}
_TAG_REGISTRY: dict[str, list[RegisteredTag]] = {}


@dataclass(frozen=True)
class RegisteredTag:
    tag: str
    owner_run_id: str
    registered_image_id: str


@dataclass(frozen=True)
class RegisteredImage:
    tag: str
    image_id: str
    owner_run_id: str


@dataclass(frozen=True)
class DockerTransportResult:
    phase: str
    command: tuple[str, ...] = field(default_factory=tuple)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    timed_out: bool = False
    timeout: int | None = None


@dataclass(frozen=True)
class ImageIdentityQuery:
    status: str
    image_id: str = ""
    error_code: str = ""
    detail: str = ""
    transport: DockerTransportResult | None = None


@dataclass(frozen=True)
class ImageCleanupResult:
    tag: str
    image_id: str
    owner_run_id: str
    removed: bool
    status: str
    error_code: str = ""
    detail: str = ""
    command: tuple[str, ...] = field(default_factory=tuple)
    command_returncode: int | None = None
    command_stdout: str = ""
    command_stderr: str = ""
    transport_phase: str = ""


@dataclass(frozen=True)
class TagCleanupResult:
    tag: str
    owner_run_id: str
    resolved_image_id: str
    removed: bool
    status: str
    error_code: str = ""
    detail: str = ""
    command: tuple[str, ...] = field(default_factory=tuple)
    command_returncode: int | None = None
    command_stdout: str = ""
    command_stderr: str = ""
    transport_phase: str = ""


@dataclass(frozen=True)
class ContainerCleanupResult:
    container_id: str
    owner_run_id: str
    removed: bool
    status: str
    error_code: str = ""
    detail: str = ""
    command: tuple[str, ...] = field(default_factory=tuple)
    command_returncode: int | None = None
    command_stdout: str = ""
    command_stderr: str = ""
    transport_phase: str = ""


@dataclass(frozen=True)
class RunTeardownResult:
    owner_run_id: str
    tag_results: list[TagCleanupResult]
    image_results: list[ImageCleanupResult]
    container_results: list[ContainerCleanupResult]
    remaining_container_count: int | None
    remaining_image_count: int | None
    remaining_image_registry_count: int
    remaining_tag_registry_count: int
    query_failures: list[str]


def parse_image_id(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("empty image id")
    if _FULL_IMAGE_ID.match(value):
        return value
    if _BARE_IMAGE_ID.match(value):
        return f"sha256:{value}"
    raise ValueError(f"invalid image id: {raw!r}")


def try_parse_image_id(raw: str) -> tuple[str | None, str]:
    try:
        return parse_image_id(raw), ""
    except ValueError as exc:
        return None, str(exc)


def _run_docker(args: list[str], *, timeout: int = 15) -> DockerTransportResult:
    command = ("docker", *args)
    try:
        proc = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return DockerTransportResult(
            phase="timed_out",
            command=command,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=str(exc),
            timed_out=True,
            timeout=timeout,
        )
    except OSError as exc:
        return DockerTransportResult(
            phase="failed_before_exec",
            command=command,
            error=str(exc),
            timeout=timeout,
        )
    return DockerTransportResult(
        phase="exited",
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timeout=timeout,
    )


def _transport_failed(transport: DockerTransportResult) -> bool:
    return transport.phase in {"failed_before_exec", "timed_out"}


def query_image_identity(image_ref: str) -> ImageIdentityQuery:
    return _query_image_identity(image_ref)


def _query_image_identity(image_ref: str) -> ImageIdentityQuery:
    transport = _run_docker(["image", "inspect", "-f", "{{.Id}}", image_ref])
    if _transport_failed(transport):
        return ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=transport.error or transport.stderr or transport.stdout,
            transport=transport,
        )
    if transport.returncode != 0 or not transport.stdout.strip():
        combined = (transport.stderr or transport.stdout).lower()
        if "no such image" in combined:
            return ImageIdentityQuery(status="absent", transport=transport)
        return ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=(transport.stderr or transport.stdout).strip(),
            transport=transport,
        )
    parsed, error = try_parse_image_id(transport.stdout.strip())
    if parsed is None:
        return ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_ID_INVALID,
            detail=error,
            transport=transport,
        )
    return ImageIdentityQuery(status="resolved", image_id=parsed, transport=transport)


def _inspect_image_labels(image_ref: str) -> tuple[dict[str, str], ImageIdentityQuery | None]:
    transport = _run_docker(["image", "inspect", "-f", "{{json .Config.Labels}}", image_ref])
    if _transport_failed(transport):
        return {}, ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=transport.error or transport.stderr or transport.stdout,
            transport=transport,
        )
    if transport.returncode != 0:
        return {}, ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=(transport.stderr or transport.stdout).strip(),
            transport=transport,
        )
    if not transport.stdout.strip():
        return {}, None
    try:
        payload = json.loads(transport.stdout.strip())
    except json.JSONDecodeError as exc:
        return {}, ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=str(exc),
            transport=transport,
        )
    if not isinstance(payload, dict):
        return {}, ImageIdentityQuery(
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail="labels payload is not an object",
            transport=transport,
        )
    return {str(key): str(value) for key, value in payload.items()}, None


def _image_presence(image_id: str) -> tuple[str, DockerTransportResult | None]:
    transport = _run_docker(["image", "inspect", image_id])
    if _transport_failed(transport):
        return "unknown", transport
    if transport.returncode == 0:
        return "present", None
    combined = (transport.stderr or transport.stdout).lower()
    if "no such image" in combined:
        return "absent", None
    return "unknown", transport


def _cleanup_result_from_transport(
    reg: RegisteredImage,
    *,
    status: str,
    error_code: str,
    detail: str,
    transport: DockerTransportResult,
) -> ImageCleanupResult:
    return ImageCleanupResult(
        tag=reg.tag,
        image_id=reg.image_id,
        owner_run_id=reg.owner_run_id,
        removed=False,
        status=status,
        error_code=error_code,
        detail=detail,
        command=transport.command,
        command_returncode=transport.returncode,
        command_stdout=transport.stdout,
        command_stderr=transport.stderr or transport.error,
        transport_phase=transport.phase,
    )


def _unregister_image(owner_run_id: str, image_id: str) -> None:
    remaining = [reg for reg in _REGISTRY.get(owner_run_id, []) if reg.image_id != image_id]
    if remaining:
        _REGISTRY[owner_run_id] = remaining
    else:
        _REGISTRY.pop(owner_run_id, None)


def _register_tag_alias(*, tag: str, owner_run_id: str, image_id: str) -> RegisteredTag:
    aliases = _TAG_REGISTRY.setdefault(owner_run_id, [])
    for existing in aliases:
        if existing.tag == tag:
            return existing
    alias = RegisteredTag(tag=tag, owner_run_id=owner_run_id, registered_image_id=image_id)
    aliases.append(alias)
    return alias


def _unregister_tag(owner_run_id: str, tag: str) -> None:
    remaining = [item for item in _TAG_REGISTRY.get(owner_run_id, []) if item.tag != tag]
    if remaining:
        _TAG_REGISTRY[owner_run_id] = remaining
    else:
        _TAG_REGISTRY.pop(owner_run_id, None)


def register_built_image(*, tag: str, owner_run_id: str) -> RegisteredImage:
    identity = _query_image_identity(tag)
    if identity.status == "query_failed":
        raise RuntimeError(
            f"failed to resolve identity for {tag}: code={identity.error_code} detail={identity.detail}"
        )
    if identity.status == "absent":
        raise RuntimeError(f"built image {tag} is absent")
    image_id = identity.image_id
    labels, label_error = _inspect_image_labels(image_id)
    if label_error is not None:
        raise RuntimeError(
            f"failed to inspect labels for image_id={image_id}: "
            f"code={label_error.error_code} detail={label_error.detail}"
        )
    label_owner = labels.get(IT_LABEL, "")
    if label_owner != owner_run_id:
        raise RuntimeError(
            f"image_id={image_id} label {IT_LABEL}={label_owner!r} "
            f"does not match owner_run_id={owner_run_id!r}"
        )
    for existing in _REGISTRY.get(owner_run_id, []):
        if existing.image_id == image_id:
            _register_tag_alias(tag=tag, owner_run_id=owner_run_id, image_id=image_id)
            return existing
    reg = RegisteredImage(tag=tag, image_id=image_id, owner_run_id=owner_run_id)
    _REGISTRY.setdefault(owner_run_id, []).append(reg)
    _register_tag_alias(tag=tag, owner_run_id=owner_run_id, image_id=image_id)
    logger.info(
        "docker_test_image_registered",
        extra={
            "action": "docker_test_image_registered",
            "status": "registered",
            "detail": {"tag": tag, "image_id": image_id, "owner_run_id": owner_run_id},
        },
    )
    return reg


def cleanup_tag_alias(alias: RegisteredTag) -> TagCleanupResult:
    identity = _query_image_identity(alias.tag)
    if identity.status == "query_failed":
        transport = identity.transport
        return TagCleanupResult(
            tag=alias.tag,
            owner_run_id=alias.owner_run_id,
            resolved_image_id="",
            removed=False,
            status="query_failed",
            error_code=identity.error_code or IMAGE_QUERY_FAILED,
            detail=identity.detail,
            command=transport.command if transport else (),
            command_returncode=transport.returncode if transport else None,
            command_stdout=transport.stdout if transport else "",
            command_stderr=(transport.stderr or transport.error) if transport else "",
            transport_phase=transport.phase if transport else "",
        )
    if identity.status == "absent":
        _unregister_tag(alias.owner_run_id, alias.tag)
        return TagCleanupResult(
            tag=alias.tag,
            owner_run_id=alias.owner_run_id,
            resolved_image_id="",
            removed=True,
            status="already_absent",
            detail="tag already absent",
        )

    resolved_image_id = identity.image_id
    labels, label_error = _inspect_image_labels(resolved_image_id)
    if label_error is not None:
        transport = label_error.transport
        return TagCleanupResult(
            tag=alias.tag,
            owner_run_id=alias.owner_run_id,
            resolved_image_id=resolved_image_id,
            removed=False,
            status="query_failed",
            error_code=label_error.error_code or IMAGE_QUERY_FAILED,
            detail=label_error.detail,
            command=transport.command if transport else (),
            command_returncode=transport.returncode if transport else None,
            command_stdout=transport.stdout if transport else "",
            command_stderr=(transport.stderr or transport.error) if transport else "",
            transport_phase=transport.phase if transport else "",
        )

    label_owner = labels.get(IT_LABEL, "")
    if label_owner != alias.owner_run_id:
        detail = (
            f"tag={alias.tag} resolves to {resolved_image_id} with {IT_LABEL}={label_owner!r} "
            f"expected owner_run_id={alias.owner_run_id!r}"
        )
        return TagCleanupResult(
            tag=alias.tag,
            owner_run_id=alias.owner_run_id,
            resolved_image_id=resolved_image_id,
            removed=False,
            status="refused",
            error_code=TAG_FOREIGN_OWNER,
            detail=detail,
        )

    transport = _run_docker(["rmi", alias.tag], timeout=60)
    if _transport_failed(transport):
        return TagCleanupResult(
            tag=alias.tag,
            owner_run_id=alias.owner_run_id,
            resolved_image_id=resolved_image_id,
            removed=False,
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=(transport.error or transport.stderr or transport.stdout).strip(),
            command=transport.command,
            command_returncode=transport.returncode,
            command_stdout=transport.stdout,
            command_stderr=transport.stderr or transport.error,
            transport_phase=transport.phase,
        )
    removed = transport.returncode == 0
    if removed:
        _unregister_tag(alias.owner_run_id, alias.tag)
        status = "untagged"
        error_code = ""
    else:
        status = "failed"
        error_code = TAG_UNTAG_FAILED
    return TagCleanupResult(
        tag=alias.tag,
        owner_run_id=alias.owner_run_id,
        resolved_image_id=resolved_image_id,
        removed=removed,
        status=status,
        error_code=error_code,
        detail=(transport.stderr or transport.stdout).strip(),
        command=transport.command,
        command_returncode=transport.returncode,
        command_stdout=transport.stdout,
        command_stderr=transport.stderr,
        transport_phase=transport.phase,
    )


def cleanup_tag_aliases_for_run(owner_run_id: str) -> list[TagCleanupResult]:
    aliases = list(_TAG_REGISTRY.get(owner_run_id, []))
    return [cleanup_tag_alias(alias) for alias in aliases]


def cleanup_registered_image(reg: RegisteredImage) -> ImageCleanupResult:
    try:
        parse_image_id(reg.image_id)
    except ValueError as exc:
        return ImageCleanupResult(
            tag=reg.tag,
            image_id=reg.image_id,
            owner_run_id=reg.owner_run_id,
            removed=False,
            status="invalid_identity",
            error_code=IMAGE_ID_INVALID,
            detail=str(exc),
        )

    presence, presence_error = _image_presence(reg.image_id)
    if presence_error is not None:
        return _cleanup_result_from_transport(
            reg,
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=(presence_error.error or presence_error.stderr or presence_error.stdout).strip(),
            transport=presence_error,
        )
    if presence == "absent":
        _unregister_image(reg.owner_run_id, reg.image_id)
        return ImageCleanupResult(
            tag=reg.tag,
            image_id=reg.image_id,
            owner_run_id=reg.owner_run_id,
            removed=True,
            status="already_absent",
            detail="image already absent",
        )

    labels, label_error = _inspect_image_labels(reg.image_id)
    if label_error is not None:
        return ImageCleanupResult(
            tag=reg.tag,
            image_id=reg.image_id,
            owner_run_id=reg.owner_run_id,
            removed=False,
            status=label_error.status if label_error.status != "resolved" else "query_failed",
            error_code=label_error.error_code or IMAGE_QUERY_FAILED,
            detail=label_error.detail,
            command=label_error.transport.command if label_error.transport else (),
            command_returncode=label_error.transport.returncode if label_error.transport else None,
            command_stdout=label_error.transport.stdout if label_error.transport else "",
            command_stderr=(
                (label_error.transport.stderr or label_error.transport.error)
                if label_error.transport
                else ""
            ),
            transport_phase=label_error.transport.phase if label_error.transport else "",
        )

    label_owner = labels.get(IT_LABEL, "")
    if label_owner != reg.owner_run_id:
        detail = (
            f"image_id={reg.image_id} label {IT_LABEL}={label_owner!r} "
            f"expected owner_run_id={reg.owner_run_id!r}"
        )
        logger.warning(
            "docker_test_image_cleanup_refused",
            extra={
                "action": "docker_test_image_cleanup_refused",
                "status": "refused",
                "detail": {
                    "error_code": IMAGE_IDENTITY_MISMATCH,
                    "tag": reg.tag,
                    "image_id": reg.image_id,
                    "owner_run_id": reg.owner_run_id,
                    "reason": detail,
                },
            },
        )
        return ImageCleanupResult(
            tag=reg.tag,
            image_id=reg.image_id,
            owner_run_id=reg.owner_run_id,
            removed=False,
            status="refused",
            error_code=IMAGE_IDENTITY_MISMATCH,
            detail=detail,
        )

    tag_identity = _query_image_identity(reg.tag)
    if tag_identity.status == "query_failed":
        return ImageCleanupResult(
            tag=reg.tag,
            image_id=reg.image_id,
            owner_run_id=reg.owner_run_id,
            removed=False,
            status="query_failed",
            error_code=tag_identity.error_code or IMAGE_QUERY_FAILED,
            detail=tag_identity.detail,
            command=tag_identity.transport.command if tag_identity.transport else (),
            command_returncode=tag_identity.transport.returncode if tag_identity.transport else None,
            command_stdout=tag_identity.transport.stdout if tag_identity.transport else "",
            command_stderr=(
                (tag_identity.transport.stderr or tag_identity.transport.error)
                if tag_identity.transport
                else ""
            ),
            transport_phase=tag_identity.transport.phase if tag_identity.transport else "",
        )
    if tag_identity.status == "resolved" and tag_identity.image_id != reg.image_id:
        detail = (
            f"tag={reg.tag} now resolves to {tag_identity.image_id} "
            f"but registered image_id={reg.image_id}"
        )
        logger.warning(
            "docker_test_image_cleanup_refused",
            extra={
                "action": "docker_test_image_cleanup_refused",
                "status": "refused",
                "detail": {
                    "error_code": IMAGE_IDENTITY_MISMATCH,
                    "tag": reg.tag,
                    "image_id": reg.image_id,
                    "owner_run_id": reg.owner_run_id,
                    "reason": detail,
                },
            },
        )
        return ImageCleanupResult(
            tag=reg.tag,
            image_id=reg.image_id,
            owner_run_id=reg.owner_run_id,
            removed=False,
            status="refused",
            error_code=IMAGE_IDENTITY_MISMATCH,
            detail=detail,
        )

    transport = _run_docker(["rmi", reg.image_id], timeout=60)
    if _transport_failed(transport):
        return _cleanup_result_from_transport(
            reg,
            status="query_failed",
            error_code=IMAGE_QUERY_FAILED,
            detail=(transport.error or transport.stderr or transport.stdout).strip(),
            transport=transport,
        )
    removed = transport.returncode == 0
    if removed:
        _unregister_image(reg.owner_run_id, reg.image_id)
        logger.info(
            "docker_test_image_removed",
            extra={
                "action": "docker_test_image_removed",
                "status": "removed",
                "detail": {
                    "tag": reg.tag,
                    "image_id": reg.image_id,
                    "owner_run_id": reg.owner_run_id,
                },
            },
        )
        status = "removed"
        error_code = ""
    else:
        status = "failed"
        error_code = IMAGE_REMOVE_FAILED
    return ImageCleanupResult(
        tag=reg.tag,
        image_id=reg.image_id,
        owner_run_id=reg.owner_run_id,
        removed=removed,
        status=status,
        error_code=error_code,
        detail=(transport.stderr or transport.stdout).strip(),
        command=transport.command,
        command_returncode=transport.returncode,
        command_stdout=transport.stdout,
        command_stderr=transport.stderr,
        transport_phase=transport.phase,
    )


def list_images_for_run(owner_run_id: str) -> tuple[list[str], DockerTransportResult | None]:
    transport = _run_docker(
        [
            "images",
            "--no-trunc",
            "-q",
            "--filter",
            f"label={IT_LABEL}={owner_run_id}",
        ]
    )
    if _transport_failed(transport):
        return [], transport
    if transport.returncode != 0:
        return [], transport
    image_ids: list[str] = []
    for line in transport.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        parsed, error = try_parse_image_id(raw)
        if parsed is None:
            return [], DockerTransportResult(
                phase="exited",
                command=transport.command,
                returncode=1,
                stdout=transport.stdout,
                stderr=f"invalid image id in list output: {error}",
            )
        if parsed in image_ids:
            continue
        image_ids.append(parsed)
    return image_ids, None


def cleanup_registered_images_for_run(owner_run_id: str) -> list[ImageCleanupResult]:
    registered = list(_REGISTRY.get(owner_run_id, []))
    results = [cleanup_registered_image(reg) for reg in registered]

    image_ids, list_error = list_images_for_run(owner_run_id)
    if list_error is not None:
        results.append(
            ImageCleanupResult(
                tag="",
                image_id="",
                owner_run_id=owner_run_id,
                removed=False,
                status="query_failed",
                error_code=IMAGE_QUERY_FAILED,
                detail=(list_error.error or list_error.stderr or list_error.stdout).strip(),
                command=list_error.command,
                command_returncode=list_error.returncode,
                command_stdout=list_error.stdout,
                command_stderr=list_error.stderr or list_error.error,
                transport_phase=list_error.phase,
            )
        )
        return results

    known_ids = {reg.image_id for reg in registered}
    for image_id in image_ids:
        if image_id in known_ids:
            continue
        fallback = RegisteredImage(tag=image_id, image_id=image_id, owner_run_id=owner_run_id)
        results.append(cleanup_registered_image(fallback))
    return results


def cleanup_images_for_run(owner_run_id: str) -> list[ImageCleanupResult]:
    cleanup_tag_aliases_for_run(owner_run_id)
    return cleanup_registered_images_for_run(owner_run_id)


def list_containers_for_run(owner_run_id: str) -> tuple[list[str], DockerTransportResult | None]:
    transport = _run_docker(["ps", "-aq", "--filter", f"label={IT_LABEL}={owner_run_id}"])
    if _transport_failed(transport):
        return [], transport
    if transport.returncode != 0:
        return [], transport
    return [line.strip() for line in transport.stdout.splitlines() if line.strip()], None


def cleanup_containers_for_run(owner_run_id: str) -> list[ContainerCleanupResult]:
    container_ids, list_error = list_containers_for_run(owner_run_id)
    if list_error is not None:
        return [
            ContainerCleanupResult(
                container_id="",
                owner_run_id=owner_run_id,
                removed=False,
                status="query_failed",
                error_code=CONTAINER_QUERY_FAILED,
                detail=(list_error.error or list_error.stderr or list_error.stdout).strip(),
                command=list_error.command,
                command_returncode=list_error.returncode,
                command_stdout=list_error.stdout,
                command_stderr=list_error.stderr or list_error.error,
                transport_phase=list_error.phase,
            )
        ]

    results: list[ContainerCleanupResult] = []
    for container_id in container_ids:
        transport = _run_docker(["rm", "-f", container_id], timeout=60)
        if _transport_failed(transport):
            results.append(
                ContainerCleanupResult(
                    container_id=container_id,
                    owner_run_id=owner_run_id,
                    removed=False,
                    status="query_failed",
                    error_code=CONTAINER_QUERY_FAILED,
                    detail=(transport.error or transport.stderr or transport.stdout).strip(),
                    command=transport.command,
                    command_returncode=transport.returncode,
                    command_stdout=transport.stdout,
                    command_stderr=transport.stderr or transport.error,
                    transport_phase=transport.phase,
                )
            )
            continue
        removed = transport.returncode == 0
        results.append(
            ContainerCleanupResult(
                container_id=container_id,
                owner_run_id=owner_run_id,
                removed=removed,
                status="removed" if removed else "failed",
                error_code="" if removed else CONTAINER_REMOVE_FAILED,
                detail=(transport.stderr or transport.stdout).strip(),
                command=transport.command,
                command_returncode=transport.returncode,
                command_stdout=transport.stdout,
                command_stderr=transport.stderr,
                transport_phase=transport.phase,
            )
        )
    return results


def finalize_run_teardown(owner_run_id: str) -> RunTeardownResult:
    container_results = cleanup_containers_for_run(owner_run_id)
    tag_results = cleanup_tag_aliases_for_run(owner_run_id)
    image_results = cleanup_registered_images_for_run(owner_run_id)

    query_failures: list[str] = []
    for result in (*container_results, *tag_results, *image_results):
        if result.status in {"query_failed", "invalid_identity"}:
            query_failures.append(result.error_code or IMAGE_QUERY_FAILED)

    remaining_container_count: int | None
    container_ids, container_list_error = list_containers_for_run(owner_run_id)
    if container_list_error is not None:
        remaining_container_count = None
        query_failures.append(CONTAINER_QUERY_FAILED)
    else:
        remaining_container_count = len(container_ids)

    remaining_image_count: int | None
    image_ids, image_list_error = list_images_for_run(owner_run_id)
    if image_list_error is not None:
        remaining_image_count = None
        query_failures.append(IMAGE_QUERY_FAILED)
    else:
        remaining_image_count = len(image_ids)

    return RunTeardownResult(
        owner_run_id=owner_run_id,
        tag_results=tag_results,
        image_results=image_results,
        container_results=container_results,
        remaining_container_count=remaining_container_count,
        remaining_image_count=remaining_image_count,
        remaining_image_registry_count=len(_REGISTRY.get(owner_run_id, [])),
        remaining_tag_registry_count=len(_TAG_REGISTRY.get(owner_run_id, [])),
        query_failures=query_failures,
    )


def assert_run_teardown_clean(result: RunTeardownResult) -> None:
    failures: list[str] = []

    for item in result.container_results:
        if item.status in {"failed", "query_failed"}:
            failures.append(
                f"container {item.container_id or '*'} status={item.status} "
                f"code={item.error_code} detail={item.detail}"
            )
    for item in result.tag_results:
        if item.status in {"failed", "query_failed", "refused"}:
            failures.append(
                f"tag {item.tag or '*'} status={item.status} "
                f"code={item.error_code} detail={item.detail}"
            )
    for item in result.image_results:
        if item.status in {"failed", "query_failed", "invalid_identity", "refused"}:
            failures.append(
                f"image {item.image_id or item.tag or '*'} status={item.status} "
                f"code={item.error_code} detail={item.detail}"
            )

    if result.query_failures:
        failures.append("query_failures=" + ",".join(sorted(set(result.query_failures))))

    if result.remaining_image_registry_count != 0:
        failures.append(
            f"remaining image registry entries for {IT_LABEL}={result.owner_run_id}: "
            f"{result.remaining_image_registry_count}"
        )
    if result.remaining_tag_registry_count != 0:
        failures.append(
            f"remaining tag registry entries for {IT_LABEL}={result.owner_run_id}: "
            f"{result.remaining_tag_registry_count}"
        )

    if result.remaining_container_count is None:
        failures.append("remaining_container_count unavailable due to docker query failure")
    elif result.remaining_container_count != 0:
        failures.append(
            f"remaining containers for {IT_LABEL}={result.owner_run_id}: "
            f"{result.remaining_container_count}"
        )

    if result.remaining_image_count is None:
        failures.append("remaining_image_count unavailable due to docker query failure")
    elif result.remaining_image_count != 0:
        failures.append(
            f"remaining images for {IT_LABEL}={result.owner_run_id}: "
            f"{result.remaining_image_count}"
        )

    if failures:
        raise AssertionError(
            f"docker test teardown incomplete for run {result.owner_run_id}: " + "; ".join(failures)
        )


def assert_tag_absent(tag: str) -> None:
    identity = _query_image_identity(tag)
    if identity.status == "query_failed":
        raise AssertionError(
            f"failed to verify tag absence for {tag}: "
            f"code={identity.error_code} detail={identity.detail}"
        )
    if identity.status != "absent":
        raise AssertionError(f"expected tag absent: {tag}")


def assert_image_absent(image_id: str) -> None:
    identity = _query_image_identity(image_id)
    if identity.status == "query_failed":
        raise AssertionError(
            f"failed to verify image absence for {image_id}: "
            f"code={identity.error_code} detail={identity.detail}"
        )
    if identity.status != "absent":
        raise AssertionError(f"expected image absent: {image_id}")


def clear_registry_for_run(owner_run_id: str) -> None:
    _REGISTRY.pop(owner_run_id, None)
    _TAG_REGISTRY.pop(owner_run_id, None)

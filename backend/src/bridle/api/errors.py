"""Custom exceptions for the Bridle API."""
from __future__ import annotations

from bridle.schemas.common import ApiError


class BridleError(Exception):
    """Base exception for all Bridle API errors.

    Carries an ApiError payload with code, message, details, resource.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
        resource: str | None = None,
    ) -> None:
        self.api_error = ApiError(
            code=code,
            message=message,
            details=details,
            resource=resource,
        )
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(BridleError):
    def __init__(self, resource: str, message: str = "Resource not found", details: dict | None = None) -> None:
        super().__init__(
            code="not_found",
            message=message,
            status_code=404,
            details=details,
            resource=resource,
        )


class ValidationError(BridleError):
    def __init__(self, resource: str, message: str = "Validation failed", details: dict | None = None) -> None:
        super().__init__(
            code="validation_error",
            message=message,
            status_code=422,
            details=details,
            resource=resource,
        )


class PlanNotExecutableError(BridleError):
    def __init__(
        self,
        *,
        last_issues: list[dict],
        rounds_used: int,
        failure_reason: str | None = None,
    ) -> None:
        super().__init__(
            code="plan_not_executable",
            message="Plan cannot be executed after complexity negotiation",
            status_code=422,
            details={
                "last_issues": last_issues,
                "rounds_used": rounds_used,
                "failure_reason": failure_reason,
            },
            resource="plan",
        )


class ConflictError(BridleError):
    def __init__(
        self,
        resource: str,
        message: str = "Conflict",
        details: dict | None = None,
        *,
        error_code: str = "conflict",
    ) -> None:
        super().__init__(
            code=error_code,
            message=message,
            status_code=409,
            details=details,
            resource=resource,
        )


class ForbiddenError(BridleError):
    def __init__(
        self,
        resource: str,
        message: str = "Forbidden",
        details: dict | None = None,
        *,
        error_code: str = "forbidden",
    ) -> None:
        super().__init__(
            code=error_code,
            message=message,
            status_code=403,
            details=details,
            resource=resource,
        )


class PayloadTooLargeError(BridleError):
    def __init__(
        self,
        resource: str,
        message: str = "Payload too large",
        details: dict | None = None,
    ) -> None:
        super().__init__(
            code="payload_too_large",
            message=message,
            status_code=413,
            details=details,
            resource=resource,
        )


class UnsupportedMediaError(BridleError):
    def __init__(
        self,
        resource: str,
        message: str = "Unsupported media type",
        details: dict | None = None,
    ) -> None:
        super().__init__(
            code="unsupported_media_type",
            message=message,
            status_code=415,
            details=details,
            resource=resource,
        )


class GatewayTimeoutError(BridleError):
    def __init__(
        self,
        resource: str,
        message: str = "Gateway timeout",
        details: dict | None = None,
        *,
        error_code: str = "gateway_timeout",
    ) -> None:
        super().__init__(
            code=error_code,
            message=message,
            status_code=504,
            details=details,
            resource=resource,
        )


class BadGatewayError(BridleError):
    def __init__(
        self,
        resource: str,
        message: str = "Bad gateway",
        details: dict | None = None,
        *,
        error_code: str = "bad_gateway",
    ) -> None:
        super().__init__(
            code=error_code,
            message=message,
            status_code=502,
            details=details,
            resource=resource,
        )

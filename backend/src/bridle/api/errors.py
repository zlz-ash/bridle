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

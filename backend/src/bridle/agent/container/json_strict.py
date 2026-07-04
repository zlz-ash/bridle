"""Strict JSON integer validation for untrusted payloads."""
from __future__ import annotations


class JsonIntError(ValueError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


def require_json_int(value: object, *, field: str) -> int:
    """Accept only JSON integers; reject bool, str, float, and compound types."""
    if isinstance(value, bool):
        raise JsonIntError("json_int_bool_rejected", detail=field)
    if value is None:
        raise JsonIntError("json_int_missing", detail=field)
    if isinstance(value, float):
        raise JsonIntError("json_int_float_rejected", detail=field)
    if isinstance(value, str):
        raise JsonIntError("json_int_string_rejected", detail=field)
    if isinstance(value, (list, dict)):
        raise JsonIntError("json_int_compound_rejected", detail=field)
    if not isinstance(value, int):
        raise JsonIntError("json_int_type_rejected", detail=field)
    return value

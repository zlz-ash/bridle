"""Tests for strict JSON integer validation."""
from __future__ import annotations

import pytest

from bridle.agent.container.json_strict import JsonIntError, require_json_int


class TestRequireJsonInt:
    @pytest.mark.parametrize("value", [1, 0, -3, 2**31])
    def test_accepts_integers(self, value: int) -> None:
        assert require_json_int(value, field="version") == value

    @pytest.mark.parametrize(
        "value,expected_code",
        [
            (True, "json_int_bool_rejected"),
            (False, "json_int_bool_rejected"),
            ("1", "json_int_string_rejected"),
            (1.0, "json_int_float_rejected"),
            (None, "json_int_missing"),
            ([1], "json_int_compound_rejected"),
            ({"v": 1}, "json_int_compound_rejected"),
        ],
    )
    def test_rejects_non_json_integers(self, value: object, expected_code: str) -> None:
        with pytest.raises(JsonIntError) as exc_info:
            require_json_int(value, field="version")
        assert exc_info.value.error_code == expected_code

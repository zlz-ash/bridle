"""Tests for mojibake detection (plan R3 / second review)."""
from __future__ import annotations

from bridle.engine.encoding_check import scan_text_for_mojibake


class TestMojibakeDetection:
    def test_detects_fengbiao_cluster(self) -> None:
        assert scan_text_for_mojibake("\u9983\u6435 mark")

    def test_detects_dash_question_mark(self) -> None:
        assert scan_text_for_mojibake("\u9225?next")

    def test_detects_checkmark_fragment(self) -> None:
        assert scan_text_for_mojibake("\u9241?ok")

    def test_detects_yuan_fragment(self) -> None:
        assert scan_text_for_mojibake("\u9286here")

    def test_allows_normal_chinese(self) -> None:
        assert not scan_text_for_mojibake("你好世界")

    def test_allows_ascii(self) -> None:
        assert not scan_text_for_mojibake("hello world")

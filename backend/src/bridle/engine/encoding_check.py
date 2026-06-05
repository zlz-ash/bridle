"""Encoding / mojibake heuristics for generated artifacts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Tokens observed in broken UTF-8 traces (plan examples: 馃搵、鉁?、鈥?).
_MOJIBAKE_LITERALS: tuple[str, ...] = (
    "\u9241",  # 鉁 (U+9241; plan trace mojibake fragment)
    "\u9983\u6435",  # 馃搵
    "\u9225",  # 鈥
    "\u9286",  # 銆 (U+9286)
)

_MOJIBAKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\u9241[\?？]?"),  # 鉁?
    re.compile(r"\u9983[\u4e00-\u9fff]?"),  # 馃 + following CJK (e.g. 搵)
    re.compile(r"\u9225[\?？]?"),  # 鈥?
    re.compile(r"\u9286"),  # 銆
)


@dataclass(frozen=True)
class EncodingRisk:
    path: str
    snippet: str
    pattern: str


def scan_text_for_mojibake(text: str, *, max_snippet: int = 80) -> list[EncodingRisk]:
    risks: list[EncodingRisk] = []
    seen: set[tuple[int, int]] = set()

    def _record(start: int, end: int, pattern: str) -> None:
        key = (start, end)
        if key in seen:
            return
        seen.add(key)
        snippet_start = max(0, start - 20)
        snippet_end = min(len(text), end + 20)
        snippet = text[snippet_start:snippet_end].replace("\n", "\\n")
        if len(snippet) > max_snippet:
            snippet = snippet[: max_snippet - 3] + "..."
        risks.append(EncodingRisk(path="", snippet=snippet, pattern=pattern))

    for literal in _MOJIBAKE_LITERALS:
        start = 0
        while True:
            index = text.find(literal, start)
            if index < 0:
                break
            _record(index, index + len(literal), f"literal:{literal!r}")
            start = index + 1

    for pattern in _MOJIBAKE_PATTERNS:
        for match in pattern.finditer(text):
            _record(match.start(), match.end(), pattern.pattern)

    return risks


def scan_file_for_mojibake(path: Path) -> list[EncodingRisk]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found = scan_text_for_mojibake(text)
    resolved = str(path.resolve())
    return [
        EncodingRisk(path=resolved, snippet=r.snippet, pattern=r.pattern) for r in found
    ]


def scan_directory_for_mojibake(
    root: Path,
    *,
    suffixes: tuple[str, ...] = (".py", ".md", ".txt", ".json", ".html", ".js", ".ts"),
) -> list[EncodingRisk]:
    if not root.exists():
        return []
    risks: list[EncodingRisk] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if suffixes and path.suffix.lower() not in suffixes:
            continue
        risks.extend(scan_file_for_mojibake(path))
    return risks

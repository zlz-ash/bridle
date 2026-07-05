"""Local static skill registry for master → worker prompt guidance."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("bridle.skill_registry")

_PACKAGE_SKILLS_DIR = Path(__file__).resolve().parent / "definitions"

_ENV_LAYOUT_FRAGMENTS: dict[str, str] = {
    "python_flat": (
        "Flat Python module layout: keep implementation modules at repo root or single package; "
        "use conftest.py for import path when tests import top-level modules."
    ),
    "python_src_package": (
        "Src layout: implementation under src/, tests under tests/; add conftest.py or "
        "pyproject.toml [tool.pytest.ini_options] pythonpath when collection fails."
    ),
    "java_spring": (
        "Java/Spring: use JUnit 5, Maven/Gradle test task, application-test.properties, "
        "and @ActiveProfiles(\"test\") for isolated DB state."
    ),
    "static_frontend": (
        "Static frontend: pytest may drive DOM checks; do not add npm build; keep assets "
        "as plain HTML/CSS/JS beside tests/."
    ),
}


class SkillLoadError(ValueError):
    """Skill definition missing or path not allowed."""


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    when_to_use: str
    submodules: dict[str, dict[str, Any]]
    prompt_fragments: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()


def detect_env_layout(
    *,
    node_type: str | None = None,
    files: list[str] | None = None,
    description: str = "",
) -> str:
    """Classify task environment for context guidance (plan item 10)."""
    paths = [str(p).replace("\\", "/") for p in (files or [])]
    lower_desc = description.lower()
    if any(p.endswith((".html", ".css", ".js")) for p in paths) or "static" in lower_desc:
        return "static_frontend"
    if node_type and str(node_type).lower() in {"java", "spring", "kotlin"}:
        return "java_spring"
    if any("src/" in p for p in paths) and any("tests/" in p for p in paths):
        return "python_src_package"
    if any(p.endswith(".py") for p in paths) and not any("src/" in p for p in paths):
        if any("/" not in p.strip("/") for p in paths if p.endswith(".py") and "test" not in p.lower()):
            return "python_flat"
    if "spring" in lower_desc or "junit" in lower_desc or "maven" in lower_desc:
        return "java_spring"
    if "src/" in lower_desc or "package" in lower_desc:
        return "python_src_package"
    return "generic"


def select_testing_submodule(
    *,
    stack: str | None = None,
    failure_kind: str | None = None,
) -> str:
    if stack and stack.lower() in {"java", "kotlin", "spring"}:
        return "testing.java"
    if failure_kind in {"import_error", "collection_error", "pytest_import"}:
        return "testing.python"
    if stack and stack.lower() in {"python", "py"}:
        return "testing.python"
    if failure_kind in {"assertion_failure", "oracle_error", "state_leakage", "command_policy"}:
        return "testing.general"
    return "testing.general"


def build_skill_guidance_for_task(
    *,
    requires_testing: bool,
    stack: str | None = None,
    failure_kind: str | None = None,
    env_layout: str | None = None,
    assigned_by: str = "master",
    assignment_reason: str | None = None,
) -> dict[str, Any]:
    if not requires_testing:
        return {"use_skill": False, "assigned_by": assigned_by}

    submodule = select_testing_submodule(stack=stack, failure_kind=failure_kind)
    registry = SkillRegistry.default()
    fragment = registry.prompt_fragment("testing", submodule)
    layout = env_layout or "generic"
    layout_hint = _ENV_LAYOUT_FRAGMENTS.get(layout, "")
    if layout_hint:
        fragment = f"{layout_hint}\n{fragment}"

    diagnosis_order = [
        "classify failure (import, assertion, isolation, policy)",
        "fix environment/layout if import/collection",
        "fix implementation or test oracle as appropriate",
        "re-run allowlisted tests",
    ]
    if submodule == "testing.python":
        diagnosis_order.insert(1, "check conftest.py / pytest.ini / src layout")
    elif submodule == "testing.java":
        diagnosis_order.insert(1, "check build file and Spring test profile")

    reason = assignment_reason or (
        f"{'Master' if assigned_by == 'master' else 'Worker'} assigned {submodule} "
        f"for stack={stack!r} layout={layout!r} failure={failure_kind!r}"
    )

    return {
        "use_skill": True,
        "assigned_by": assigned_by,
        "skill_id": "testing",
        "submodule": submodule,
        "env_layout": layout,
        "reason": reason,
        "prompt_fragment": fragment,
        "diagnosis_order": diagnosis_order,
    }


class SkillRegistry:
    def __init__(self, skills: dict[str, SkillDefinition]) -> None:
        self._skills = skills

    @classmethod
    def default(cls) -> SkillRegistry:
        return cls.from_paths([_PACKAGE_SKILLS_DIR])

    @classmethod
    def from_paths(cls, roots: list[Path]) -> SkillRegistry:
        skills: dict[str, SkillDefinition] = {}
        for root in roots:
            resolved_root = root.resolve()
            if resolved_root.is_file():
                raise SkillLoadError(f"skill root must be a directory: {resolved_root}")
            if not resolved_root.is_dir():
                raise SkillLoadError(f"skill root not found: {resolved_root}")
            for path in sorted(resolved_root.rglob("*.json")):
                resolved_path = path.resolve()
                if not resolved_path.is_relative_to(resolved_root):
                    raise SkillLoadError(f"skill path outside root: {path}")
                definition = _load_skill_file(resolved_path)
                skills[definition.id] = definition
        if not skills:
            raise SkillLoadError("no skills loaded")
        return cls(skills)

    def get(self, skill_id: str) -> SkillDefinition:
        if skill_id not in self._skills:
            raise SkillLoadError(f"unknown skill: {skill_id}")
        return self._skills[skill_id]

    def list_ids(self) -> list[str]:
        """List shared skills; no input exits as stable registry identifiers."""
        return sorted(self._skills)

    def prompt_fragment(self, skill_id: str, submodule_key: str) -> str:
        skill = self.get(skill_id)
        sub = skill.submodules.get(submodule_key)
        if sub is None:
            raise SkillLoadError(f"unknown submodule {submodule_key!r} for {skill_id!r}")
        fragments = sub.get("prompt_fragments") or []
        if not isinstance(fragments, list):
            raise SkillLoadError(f"invalid prompt_fragments for {submodule_key!r}")
        lines = [str(line) for line in fragments if str(line).strip()]
        if skill.prompt_fragments:
            lines = list(skill.prompt_fragments) + lines
        return "\n".join(lines)


def _load_skill_file(path: Path) -> SkillDefinition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillLoadError(f"failed to load skill file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillLoadError(f"skill file must be object: {path}")
    submodules = raw.get("submodules") or {}
    if not isinstance(submodules, dict):
        raise SkillLoadError(f"invalid submodules in {path}")
    top_fragments = raw.get("prompt_fragments") or []
    evidence = raw.get("evidence_requirements") or []
    return SkillDefinition(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        when_to_use=str(raw.get("when_to_use", "")),
        submodules=submodules,
        prompt_fragments=tuple(str(x) for x in top_fragments) if isinstance(top_fragments, list) else (),
        evidence_requirements=tuple(str(x) for x in evidence) if isinstance(evidence, list) else (),
    )

"""Tests for skill registry and testing skill selection."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.skill_registry import (
    SkillLoadError,
    SkillRegistry,
    build_skill_guidance_for_task,
    select_testing_submodule,
)


class TestSkillRegistry:
    def test_loads_testing_skill(self) -> None:
        registry = SkillRegistry.default()
        skill = registry.get("testing")
        assert skill.id == "testing"
        assert "testing.python" in skill.submodules
        assert "testing.java" in skill.submodules
        assert "testing.general" in skill.submodules

    def test_unknown_skill_rejected(self) -> None:
        registry = SkillRegistry.default()
        with pytest.raises(SkillLoadError):
            registry.get("nonexistent")

    def test_submodule_prompt_fragment(self) -> None:
        registry = SkillRegistry.default()
        fragment = registry.prompt_fragment("testing", "testing.python")
        assert "pytest" in fragment.lower() or "conftest" in fragment.lower()

    def test_skill_root_must_be_directory(self, tmp_path: Path) -> None:
        evil = tmp_path / "evil.json"
        evil.write_text("{}", encoding="utf-8")
        with pytest.raises(SkillLoadError):
            SkillRegistry.from_paths([evil])

    def test_detect_env_layout_variants(self) -> None:
        from bridle.engine.skill_registry import detect_env_layout

        assert detect_env_layout(files=["cli.py", "tests/test_cli.py"]) == "python_flat"
        assert (
            detect_env_layout(files=["src/pkg/a.py", "tests/test_a.py"])
            == "python_src_package"
        )
        assert detect_env_layout(files=["index.html", "app.js"]) == "static_frontend"
        assert detect_env_layout(node_type="java", files=["pom.xml"]) == "java_spring"

    def test_select_testing_submodule_import_error(self) -> None:
        assert select_testing_submodule(failure_kind="import_error") == "testing.python"

    def test_select_testing_submodule_assertion(self) -> None:
        assert select_testing_submodule(failure_kind="assertion_failure") == "testing.general"

    def test_select_testing_submodule_java_stack(self) -> None:
        assert select_testing_submodule(stack="java", failure_kind="assertion_failure") == "testing.java"

    def test_build_skill_guidance_for_python_task(self) -> None:
        guidance = build_skill_guidance_for_task(
            requires_testing=True,
            stack="python",
            failure_kind=None,
        )
        assert guidance["use_skill"] is True
        assert guidance["skill_id"] == "testing"
        assert guidance["submodule"] == "testing.python"
        assert "reason" in guidance
        assert isinstance(guidance["diagnosis_order"], list)

    def test_build_skill_guidance_skipped_when_not_needed(self) -> None:
        guidance = build_skill_guidance_for_task(requires_testing=False)
        assert guidance["use_skill"] is False


class TestSkillGuidanceInContext:
    def test_context_template_includes_skill_guidance(self) -> None:
        import json

        from bridle.engine.context_template import ContextTemplateBuilder
        from bridle.schemas.proposal import AgentContext

        ctx = AgentContext(
            instruction="Fix tests",
            node={"id": "n1", "title": "T", "goal": "Fix"},
            allowed_files=["src/a.py"],
            tests=["pytest -q"],
            accessible_context={
                "skill_guidance": build_skill_guidance_for_task(
                    requires_testing=True,
                    stack="python",
                ),
            },
        )
        payload = ContextTemplateBuilder(ctx).build_payload()
        dumped = json.dumps(payload.model_dump(), ensure_ascii=False)
        assert "skill_guidance" in dumped
        assert "testing.python" in dumped

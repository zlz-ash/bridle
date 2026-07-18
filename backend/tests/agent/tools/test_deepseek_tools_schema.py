from bridle.agent.tools.deepseek_schema import V1_TOOL_NAMES, build_deepseek_tools


def _names(tools: list[dict]) -> list[str]:
    return [item["function"]["name"] for item in tools]


def test_default_schema_exposes_only_minimal_model_tools() -> None:
    assert V1_TOOL_NAMES == ("run_command", "report_blocked", "web_search")
    assert _names(build_deepseek_tools()) == list(V1_TOOL_NAMES)


def test_runtime_schema_is_enabled_from_the_same_catalog() -> None:
    enabled = {
        "read_project_map",
        "patch_plan_nodes",
        "execute_plan_node",
    }

    assert _names(build_deepseek_tools(enabled_names=enabled)) == [
        "read_project_map",
        "patch_plan_nodes",
        "execute_plan_node",
    ]


def test_every_schema_is_closed_and_strict_flag_is_optional() -> None:
    loose = build_deepseek_tools(enabled_names=set(V1_TOOL_NAMES), strict=False)
    strict = build_deepseek_tools(enabled_names=set(V1_TOOL_NAMES), strict=True)

    for item in loose:
        assert item["function"]["parameters"]["additionalProperties"] is False
        assert "strict" not in item["function"]
    for item in strict:
        assert item["function"]["strict"] is True


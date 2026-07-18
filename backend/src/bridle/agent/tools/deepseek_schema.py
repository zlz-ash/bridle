"""DeepSeek-compatible OpenAI function tool schemas."""
from __future__ import annotations

V1_TOOL_NAMES = (
    "run_command",
    "report_blocked",
    "web_search",
)


def _function_tool(
    name: str,
    description: str,
    properties: dict,
    required: list[str],
    *,
    strict: bool,
) -> dict:
    fn: dict = {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }
    if strict:
        fn["strict"] = True
    return {"type": "function", "function": fn}


def build_deepseek_tools(
    *,
    strict: bool = False,
    enabled_names: set[str] | None = None,
) -> list[dict]:
    """Build v1 tool definitions for DeepSeek chat completions."""
    tools = [
        _function_tool(
            "run_command",
            "Run an exploratory Bash command inside the isolated candidate container.",
            {"command": {"type": "string"}},
            ["command"],
            strict=strict,
        ),
        _function_tool(
            "report_blocked",
            "Report a blocking issue without changing node status.",
            {
                "reason": {"type": "string"},
                "evidence": {"type": "object"},
            },
            ["reason", "evidence"],
            strict=strict,
        ),
        _function_tool(
            "web_search",
            "Search the web for documentation, error explanations, or reference material.",
            {
                "query": {"type": "string"},
                "allowed_domains": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer"},
            },
            ["query"],
            strict=strict,
        ),
        _function_tool(
            "read_project_map",
            "Read one bounded view from the project SQLite plan map.",
            {
                "mode": {"type": "string", "enum": ["overview", "node", "children", "subgraph", "search", "execution"]},
                "node_id": {"type": "string"},
                "wait_id": {"type": "string"},
                "parent_id": {"type": ["string", "null"]},
                "query": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
                "depth": {"type": "integer"},
            },
            ["mode"],
            strict=strict,
        ),
        _function_tool(
            "patch_plan_nodes",
            "Apply the existing local PlanPatchSchema to editable project nodes.",
            {
                "update_nodes": {"type": "array", "items": {"type": "object"}},
                "add_nodes": {"type": "array", "items": {"type": "object"}},
                "remove_node_ids": {"type": "array", "items": {"type": "string"}},
                "replace_dependencies": {"type": "array", "items": {"type": "object"}},
            },
            [],
            strict=strict,
        ),
        _function_tool(
            "execute_plan_node",
            "Create or reuse a durable background plan-node workflow and return its wait signal.",
            {"node_id": {"type": "string"}},
            ["node_id"],
            strict=strict,
        ),
    ]
    if enabled_names is None:
        return [tool for tool in tools if tool["function"]["name"] in V1_TOOL_NAMES]
    return [tool for tool in tools if tool["function"]["name"] in enabled_names]


def tool_names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}

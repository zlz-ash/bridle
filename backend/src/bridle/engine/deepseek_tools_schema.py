"""DeepSeek-compatible OpenAI function tool schemas."""
from __future__ import annotations

V1_TOOL_NAMES = (
    "read_allowed_file",
    "propose_file_patch",
    "run_allowed_tests",
    "report_blocked",
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


def build_deepseek_tools(*, strict: bool = False) -> list[dict]:
    """Build v1 tool definitions for DeepSeek chat completions."""
    return [
        _function_tool(
            "read_allowed_file",
            "Read one file that is explicitly allowed for this node run.",
            {"path": {"type": "string"}},
            ["path"],
            strict=strict,
        ),
        _function_tool(
            "propose_file_patch",
            "Propose a patch for an allowed file without writing to disk.",
            {
                "path": {"type": "string"},
                "change_type": {"type": "string"},
                "diff": {"type": "string"},
            },
            ["path", "change_type", "diff"],
            strict=strict,
        ),
        _function_tool(
            "run_allowed_tests",
            "Run test commands from the node allowlist via sandbox policy.",
            {
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            ["commands"],
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
    ]


def tool_names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}

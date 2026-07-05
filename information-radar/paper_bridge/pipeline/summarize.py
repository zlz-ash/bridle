"""AI 摘要：证据约束的中文结构化摘要。

业务契约（来自方案）：
- 输出字段：研究问题/Agent循环与工具/Context记忆规划/数据集基准/主要结果/成本延迟/失败模式局限/推荐
- 没有开放全文时必须标记"仅根据标题和摘要整理"
- 不允许补写未报告内容，未报告字段填"未报告"
- 不编造数字
- 推荐判定：值得复现/值得了解/低优先级
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from paper_bridge.models import Item

# 摘要结果的标准字段（业务契约）
SUMMARY_FIELDS = [
    "research_question",
    "agent_loop_and_tools",
    "context_memory_planning",
    "dataset_or_benchmark",
    "main_results",
    "cost_and_latency",
    "failure_modes_and_limitations",
    "recommendation",
]

NO_FULL_TEXT_MARKER = "[仅根据标题和摘要整理]"


@dataclass
class PaperSummary:
    """单篇论文的结构化摘要结果。"""
    research_question: str = "未报告"
    agent_loop_and_tools: str = "未报告"
    context_memory_planning: str = "未报告"
    dataset_or_benchmark: str = "未报告"
    main_results: str = "未报告"
    cost_and_latency: str = "未报告"
    failure_modes_and_limitations: str = "未报告"
    recommendation: str = "低优先级"
    has_full_text: bool = False
    marked_partial: bool = False
    raw_response: str = ""

    def to_dict(self) -> dict:
        d = {f: getattr(self, f) for f in SUMMARY_FIELDS}
        d["has_full_text"] = self.has_full_text
        if self.marked_partial:
            d["_partial_marker"] = NO_FULL_TEXT_MARKER
        return d


def _load_prompt_template(path: str | Path = "config/prompts/summary.md") -> str:
    """加载 prompt 模板原文（用于审计）。"""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def build_user_prompt(item: Item) -> str:
    """根据 Item 构造用户提示。"""
    authors = ", ".join(item.authors) if item.authors else "未报告"
    affiliations = ", ".join(item.affiliations) if item.affiliations else "未报告"
    date = item.published_at.strftime("%Y-%m-%d") if item.published_at else "未报告"
    venue = item.venue or "未报告"
    status = "预印本" if item.source_type == "arxiv" else "正式论文"
    abstract = item.abstract or "未报告"

    return (
        f"标题：{item.title}\n"
        f"作者：{authors}\n"
        f"机构：{affiliations}\n"
        f"日期：{date}\n"
        f"会议/期刊：{venue}\n"
        f"状态：{status}\n"
        f"摘要：{abstract}\n"
        f"开放全文：{'true' if item.has_full_text else 'false'}\n"
    )


def _extract_json(text: str) -> dict | None:
    """从模型回复中提取 JSON 对象（容忍 ```json 包裹）。"""
    # 去除 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # 尝试直接找 JSON 对象
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def parse_summary(response_text: str, has_full_text: bool) -> PaperSummary:
    """把模型回复解析为 PaperSummary。

    契约：
    - 无开放全文时 marked_partial=True
    - 缺失字段填"未报告"
    - recommendation 规范化为三个值之一
    """
    summary = PaperSummary(has_full_text=has_full_text)
    summary.raw_response = response_text

    data = _extract_json(response_text)
    if data is None:
        logger.warning("failed to extract JSON from AI response, using defaults")
        summary.marked_partial = not has_full_text
        return summary

    for field_name in SUMMARY_FIELDS:
        val = data.get(field_name)
        if val and isinstance(val, str) and val.strip():
            setattr(summary, field_name, val.strip())
        else:
            setattr(summary, field_name, "未报告")

    # 推荐规范化
    rec = summary.recommendation
    if "复现" in rec:
        summary.recommendation = "值得复现"
    elif "了解" in rec:
        summary.recommendation = "值得了解"
    else:
        summary.recommendation = "低优先级"

    # 无开放全文标记
    summary.marked_partial = not has_full_text
    return summary


class Summarizer:
    """AI 摘要器。

    使用 OpenAI SDK（便于后续切换底层模型）。
    费用控制：累计 cost 超过 limit 则中止。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        max_tokens: int = 1200,
        cost_limit_usd: float = 2.0,
        system_prompt: str | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.cost_limit_usd = cost_limit_usd
        self.system_prompt = system_prompt or _default_system_prompt()
        self.total_cost_usd: float = 0.0
        self.total_tokens: int = 0
        self.call_count: int = 0
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def summarize(self, item: Item) -> PaperSummary:
        """对单篇论文生成结构化摘要。

        失败时返回带"未报告"默认值的 PaperSummary，不抛异常（单篇失败不影响批次）。
        """
        if self.total_cost_usd >= self.cost_limit_usd:
            logger.warning(
                "cost limit reached: ${:.4f} >= ${:.2f}, skipping summary for: {}",
                self.total_cost_usd,
                self.cost_limit_usd,
                item.title[:50],
            )
            s = PaperSummary(has_full_text=item.has_full_text)
            s.marked_partial = not item.has_full_text
            s.research_question = "[费用超限，未生成摘要]"
            return s

        user_prompt = build_user_prompt(item)
        logger.debug("summarizing: {} (full_text={})", item.title[:50], item.has_full_text)

        try:
            client = self._get_client()
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=self.max_tokens,
                temperature=0.2,
            )
            text = resp.choices[0].message.content or ""
            self.call_count += 1

            # 估算费用（粗略）
            usage = resp.usage
            if usage:
                self.total_tokens += usage.total_tokens
                self.total_cost_usd += self._estimate_cost(usage.prompt_tokens, usage.completion_tokens)

            logger.info(
                "summarized: {} tokens={} cost=${:.4f} total_cost=${:.4f}",
                item.title[:50],
                usage.total_tokens if usage else 0,
                self._estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else 0,
                self.total_cost_usd,
            )

            return parse_summary(text, item.has_full_text)

        except Exception as e:
            logger.error("summarize failed for {}: {}", item.title[:50], e)
            s = PaperSummary(has_full_text=item.has_full_text)
            s.marked_partial = not item.has_full_text
            s.research_question = f"[摘要生成失败: {type(e).__name__}]"
            return s

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """粗略费用估算（gpt-4o-mini 价格：$0.15/1M input, $0.60/1M output）。"""
        return (prompt_tokens * 0.15 + completion_tokens * 0.60) / 1_000_000

    def stats(self) -> dict:
        return {
            "call_count": self.call_count,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
        }


def _default_system_prompt() -> str:
    """内置默认系统提示（与 config/prompts/summary.md 一致）。"""
    return """你是一名软件工程与 AI Agent 实践型研究的资深分析师。你的任务是基于**给定证据**生成结构化中文摘要。

### 铁律（违反即失败）
1. **证据约束**：只允许使用输入中明确提供的标题、摘要、作者、元数据。禁止补写任何未在输入中报告的内容。
2. **开放全文缺失标记**：当输入未包含开放全文（仅有标题+摘要）时，必须在摘要末尾输出固定标记：`[仅根据标题和摘要整理]`，且不得推测方法细节、实验结果、成本与失败模式。
3. **不编造数字**：若输入未给出具体指标、成本、延迟、失败率，对应字段写"未报告"，不得填入臆测值。
4. **客观中立**：不夸大、不营销化。

### 输出格式（严格 JSON，字段不可省略）

```json
{
  "research_question": "研究问题（1-2 句）",
  "agent_loop_and_tools": "Agent 工作循环与工具调用方式；若非 Agent 类论文或无证据，填\"未报告\"",
  "context_memory_planning": "Context / 记忆 / 规划方式；无证据填\"未报告\"",
  "dataset_or_benchmark": "数据集 / 真实仓库 / 评测基准；无填\"未报告\"",
  "main_results": "主要结果；未报告填\"未报告\"",
  "cost_and_latency": "成本与延迟；未报告填\"未报告\"",
  "failure_modes_and_limitations": "失败模式与局限；未报告填\"未报告\"",
  "recommendation": "值得复现 | 值得了解 | 低优先级"
}
```

### 推荐判定标准
- 值得复现：有真实仓库/PR + 可执行基准 + 公开代码/数据，且结果可核验。
- 值得了解：证据部分缺失但主题与方法有参考价值。
- 低优先级：纯概念、玩具题、无执行证据、与 SE 实践弱相关。"""

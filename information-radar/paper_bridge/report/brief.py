"""三联简报生成：Markdown / HTML / JSON。

业务契约（来自方案）：
- 每篇论文输出：标题/作者/机构/日期/会议/状态/研究问题/Agent循环/Context记忆/
  数据集基准/主要结果/成本延迟/失败模式/代码数据PDF DOI/推荐
- 无开放全文标记"仅根据标题和摘要整理"
- 三联格式：MD（飞书/阅读）、HTML（富文本）、JSON（程序消费）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from paper_bridge.models import Item
from paper_bridge.pipeline.scoring import ScoreBreakdown
from paper_bridge.pipeline.summarize import PaperSummary


class BriefItem(BaseModel):
    """简报中的单条目。"""
    title: str
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    date: str | None = None
    venue: str | None = None
    status: str = "预印本"
    source_type: str = ""
    url: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    pdf_url: str | None = None
    has_full_text: bool = False

    # 评分
    total_score: int = 0
    tier: str = "audit_only"
    score_breakdown: dict = Field(default_factory=dict)

    # AI 摘要
    summary: dict = Field(default_factory=dict)
    partial_marker: str | None = None

    @classmethod
    def from_data(
        cls, item: Item, score: ScoreBreakdown, summary: PaperSummary
    ) -> BriefItem:
        return cls(
            title=item.title,
            authors=item.authors,
            affiliations=item.affiliations,
            date=item.published_at.strftime("%Y-%m-%d") if item.published_at else None,
            venue=item.venue,
            status="预印本" if item.source_type == "arxiv" else "正式论文",
            source_type=item.source_type,
            url=item.url,
            doi=item.doi,
            arxiv_id=item.arxiv_id,
            pdf_url=item.full_text_url,
            has_full_text=item.has_full_text,
            total_score=score.total,
            tier=score.tier,
            score_breakdown={
                "domain_relevance": score.domain_relevance,
                "practical_value": score.practical_value,
                "evidence_quality": score.evidence_quality,
                "reproducibility": score.reproducibility,
                "timeliness": score.timeliness,
                "bonus_applied": score.bonus_applied,
            },
            summary=summary.to_dict(),
            partial_marker="[仅根据标题和摘要整理]" if summary.marked_partial else None,
        )


class Brief(BaseModel):
    """每日简报。"""
    brief_id: str
    run_id: str
    date: str
    generated_at: str
    stats: dict = Field(default_factory=dict)
    items: list[BriefItem] = Field(default_factory=list)

    def selected(self) -> list[BriefItem]:
        return [i for i in self.items if i.tier == "selected"]

    def archived(self) -> list[BriefItem]:
        return [i for i in self.items if i.tier == "archived"]


def build_brief(
    scored: list[tuple[Item, ScoreBreakdown]],
    summaries: dict[int, PaperSummary],
    run_id: str,
    stats: dict | None = None,
) -> Brief:
    """从评分结果 + 摘要构建简报。

    summaries 的 key 是 item 在 scored 列表中的索引。
    """
    brief_id = f"brief-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    items: list[BriefItem] = []
    for idx, (item, score) in enumerate(scored):
        summary = summaries.get(idx, PaperSummary(has_full_text=item.has_full_text))
        items.append(BriefItem.from_data(item, score, summary))

    brief = Brief(
        brief_id=brief_id,
        run_id=run_id,
        date=datetime.now().strftime("%Y-%m-%d"),
        generated_at=datetime.now().isoformat(),
        stats=stats or {},
        items=items,
    )
    logger.info(
        "brief built: id={} items={} selected={} archived={}",
        brief.brief_id,
        len(items),
        len(brief.selected()),
        len(brief.archived()),
    )
    return brief


# ============ JSON 导出 ============

def to_json(brief: Brief) -> str:
    """导出 JSON 格式简报。"""
    return json.dumps(brief.model_dump(), ensure_ascii=False, indent=2)


# ============ Markdown 导出 ============

def to_markdown(brief: Brief) -> str:
    """导出 Markdown 格式简报。"""
    lines: list[str] = []
    lines.append(f"# 信息雷达每日简报 {brief.date}")
    lines.append("")
    lines.append(f"- 简报 ID：`{brief.brief_id}`")
    lines.append(f"- 运行 ID：`{brief.run_id}`")
    lines.append(f"- 生成时间：{brief.generated_at}")
    if brief.stats:
        lines.append(f"- 统计：输入 {brief.stats.get('items_in', 0)} | 去重 {brief.stats.get('items_dedup', 0)} | "
                     f"过滤 {brief.stats.get('items_filtered', 0)} | 入选 {brief.stats.get('items_selected', 0)}")
    lines.append("")

    selected = brief.selected()
    if selected:
        lines.append(f"## 每日精选（{len(selected)} 篇）")
        lines.append("")
        for i, item in enumerate(selected, 1):
            lines.append(_item_to_markdown(i, item))

    archived = brief.archived()
    if archived:
        lines.append(f"## 归档备查（{len(archived)} 篇）")
        lines.append("")
        for i, item in enumerate(archived, 1):
            lines.append(_item_to_markdown(i, item))

    audit_only = [i for i in brief.items if i.tier == "audit_only"]
    if audit_only:
        lines.append(f"## 审计记录（{len(audit_only)} 篇，仅保留记录）")
        lines.append("")
        for i, item in enumerate(audit_only, 1):
            lines.append(_audit_item_to_markdown(i, item))

    if not selected and not archived and not audit_only:
        lines.append("## 今日无新增精选内容")
        lines.append("")

    return "\n".join(lines)


def _audit_item_to_markdown(idx: int, item: BriefItem) -> str:
    """审计记录条目（简略形式，含 partial_marker）。"""
    lines: list[str] = []
    lines.append(f"### {idx}. {item.title}")
    lines.append(f"- 评分：{item.total_score}/100（audit_only） | 链接：{item.url}")
    if not item.has_full_text and item.partial_marker:
        lines.append(f"- **⚠️ {item.partial_marker}**")
    lines.append("")
    return "\n".join(lines)


def _item_to_markdown(idx: int, item: BriefItem) -> str:
    """单条目 Markdown。"""
    lines: list[str] = []
    lines.append(f"### {idx}. {item.title}")
    lines.append("")
    lines.append(f"- **作者**：{', '.join(item.authors) if item.authors else '未报告'}")
    lines.append(f"- **机构**：{', '.join(item.affiliations) if item.affiliations else '未报告'}")
    lines.append(f"- **日期**：{item.date or '未报告'}")
    lines.append(f"- **会议/期刊**：{item.venue or '未报告'}")
    lines.append(f"- **状态**：{item.status}")
    lines.append(f"- **评分**：{item.total_score}/100（{item.tier}）")
    lines.append(f"- **链接**：{item.url}")
    if item.doi:
        lines.append(f"- **DOI**：`{item.doi}`")
    if item.arxiv_id:
        lines.append(f"- **arXiv ID**：`{item.arxiv_id}`")
    if item.pdf_url:
        lines.append(f"- **PDF**：{item.pdf_url}")
    if not item.has_full_text and item.partial_marker:
        lines.append(f"- **⚠️ {item.partial_marker}**")
    lines.append("")
    s = item.summary
    lines.append(f"- **研究问题**：{s.get('research_question', '未报告')}")
    lines.append(f"- **Agent 循环与工具**：{s.get('agent_loop_and_tools', '未报告')}")
    lines.append(f"- **Context/记忆/规划**：{s.get('context_memory_planning', '未报告')}")
    lines.append(f"- **数据集/基准**：{s.get('dataset_or_benchmark', '未报告')}")
    lines.append(f"- **主要结果**：{s.get('main_results', '未报告')}")
    lines.append(f"- **成本与延迟**：{s.get('cost_and_latency', '未报告')}")
    lines.append(f"- **失败模式与局限**：{s.get('failure_modes_and_limitations', '未报告')}")
    lines.append(f"- **推荐**：**{s.get('recommendation', '低优先级')}**")
    lines.append("")
    return "\n".join(lines)


# ============ HTML 导出 ============

def to_html(brief: Brief) -> str:
    """导出 HTML 格式简报（内联 CSS，适合飞书/邮件）。"""
    selected = brief.selected()
    archived = brief.archived()

    items_html: list[str] = []
    if selected:
        items_html.append(f'<h2>每日精选（{len(selected)} 篇）</h2>')
        for i, item in enumerate(selected, 1):
            items_html.append(_item_to_html(i, item))
    if archived:
        items_html.append(f'<h2>归档备查（{len(archived)} 篇）</h2>')
        for i, item in enumerate(archived, 1):
            items_html.append(_item_to_html(i, item))
    audit_only = [i for i in brief.items if i.tier == "audit_only"]
    if audit_only:
        items_html.append(f'<h2>审计记录（{len(audit_only)} 篇，仅保留记录）</h2>')
        for i, item in enumerate(audit_only, 1):
            items_html.append(_audit_item_to_html(i, item))
    if not selected and not archived and not audit_only:
        items_html.append('<h2>今日无新增精选内容</h2>')

    stats_html = ""
    if brief.stats:
        stats_html = (
            f'<div class="stats">输入 {brief.stats.get("items_in", 0)} | '
            f"去重 {brief.stats.get('items_dedup', 0)} | "
            f"过滤 {brief.stats.get('items_filtered', 0)} | "
            f"入选 {brief.stats.get('items_selected', 0)}</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>信息雷达每日简报 {brief.date}</title>
<style>
  body {{ font-family: -apple-system, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
         max-width: 900px; margin: 0 auto; padding: 20px; color: #333; line-height: 1.6; }}
  h1 {{ color: #1a1a2e; border-bottom: 2px solid #16213e; padding-bottom: 10px; }}
  h2 {{ color: #16213e; margin-top: 30px; }}
  h3 {{ color: #0f3460; }}
  .meta {{ color: #666; font-size: 14px; margin-bottom: 5px; }}
  .stats {{ background: #f0f4ff; padding: 10px; border-radius: 5px; margin: 15px 0; }}
  .item {{ border-left: 3px solid #16213e; padding-left: 15px; margin: 20px 0; }}
  .item.selected {{ border-left-color: #e94560; }}
  .item.archived {{ border-left-color: #999; opacity: 0.85; }}
  .score {{ background: #e94560; color: white; padding: 2px 8px; border-radius: 3px; font-size: 13px; }}
  .score.archived {{ background: #999; }}
  .partial {{ background: #fff3cd; padding: 5px 10px; border-radius: 3px; color: #856404; font-size: 13px; }}
  .summary-field {{ margin: 4px 0; }}
  .summary-field strong {{ color: #0f3460; }}
  .recommendation {{ font-weight: bold; color: #e94560; }}
  a {{ color: #0f3460; }}
</style>
</head>
<body>
<h1>信息雷达每日简报 {brief.date}</h1>
<div class="meta">
  简报 ID：<code>{brief.brief_id}</code> | 运行 ID：<code>{brief.run_id}</code><br>
  生成时间：{brief.generated_at}
</div>
{stats_html}
{chr(10).join(items_html)}
</body>
</html>"""


def _item_to_html(idx: int, item: BriefItem) -> str:
    """单条目 HTML。"""
    s = item.summary
    tier_class = "selected" if item.tier == "selected" else "archived"
    partial_html = (
        f'<div class="partial">⚠️ {item.partial_marker}</div>'
        if not item.has_full_text and item.partial_marker
        else ""
    )
    doi_html = f'<div class="summary-field"><strong>DOI：</strong><code>{item.doi}</code></div>' if item.doi else ""
    arxiv_html = f'<div class="summary-field"><strong>arXiv ID：</strong><code>{item.arxiv_id}</code></div>' if item.arxiv_id else ""
    pdf_html = f'<div class="summary-field"><strong>PDF：</strong><a href="{item.pdf_url}">{item.pdf_url}</a></div>' if item.pdf_url else ""

    return f"""<div class="item {tier_class}">
<h3>{idx}. {item.title}</h3>
<div class="summary-field"><strong>作者：</strong>{', '.join(item.authors) if item.authors else '未报告'}</div>
<div class="summary-field"><strong>机构：</strong>{', '.join(item.affiliations) if item.affiliations else '未报告'}</div>
<div class="summary-field"><strong>日期：</strong>{item.date or '未报告'} | <strong>会议：</strong>{item.venue or '未报告'} | <strong>状态：</strong>{item.status}</div>
<div class="summary-field"><strong>评分：</strong><span class="score {tier_class}">{item.total_score}/100</span>（{item.tier}）</div>
<div class="summary-field"><strong>链接：</strong><a href="{item.url}">{item.url}</a></div>
{doi_html}
{arxiv_html}
{pdf_html}
{partial_html}
<div class="summary-field"><strong>研究问题：</strong>{s.get('research_question', '未报告')}</div>
<div class="summary-field"><strong>Agent 循环与工具：</strong>{s.get('agent_loop_and_tools', '未报告')}</div>
<div class="summary-field"><strong>Context/记忆/规划：</strong>{s.get('context_memory_planning', '未报告')}</div>
<div class="summary-field"><strong>数据集/基准：</strong>{s.get('dataset_or_benchmark', '未报告')}</div>
<div class="summary-field"><strong>主要结果：</strong>{s.get('main_results', '未报告')}</div>
<div class="summary-field"><strong>成本与延迟：</strong>{s.get('cost_and_latency', '未报告')}</div>
<div class="summary-field"><strong>失败模式与局限：</strong>{s.get('failure_modes_and_limitations', '未报告')}</div>
<div class="summary-field"><strong>推荐：</strong><span class="recommendation">{s.get('recommendation', '低优先级')}</span></div>
</div>"""


def _audit_item_to_html(idx: int, item: BriefItem) -> str:
    """审计记录条目 HTML（简略，含 partial_marker）。"""
    partial_html = (
        f'<div class="partial">⚠️ {item.partial_marker}</div>'
        if not item.has_full_text and item.partial_marker
        else ""
    )
    return f"""<div class="item archived">
<h3>{idx}. {item.title}</h3>
<div class="summary-field">评分：<span class="score archived">{item.total_score}/100</span>（audit_only） | <a href="{item.url}">{item.url}</a></div>
{partial_html}
</div>"""


def save_brief(brief: Brief, output_dir: str | Path) -> dict[str, Path]:
    """把三联简报保存到目录，返回 {format: path}。"""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / brief.brief_id
    paths = {
        "json": Path(f"{base}.json"),
        "markdown": Path(f"{base}.md"),
        "html": Path(f"{base}.html"),
    }
    paths["json"].write_text(to_json(brief), encoding="utf-8")
    paths["markdown"].write_text(to_markdown(brief), encoding="utf-8")
    paths["html"].write_text(to_html(brief), encoding="utf-8")
    logger.info("brief saved: {} (json/md/html)", brief.brief_id)
    return paths

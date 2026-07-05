"""统一条目模型 —— 所有来源采集后归一化为此结构。

这是整条管线的**数据契约**：来源层负责产出 Item，去重/评分/摘要层只消费 Item。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal["blog", "bilibili", "wechat_mp", "arxiv", "crossref", "semantic_scholar"]
Category = Literal["engineering", "ai", "tech", "se_paper"]


def normalize_title(title: str) -> str:
    """标题归一化：NFKC + 小写 + 去标点 + 压缩空白。

    去重时标题指纹基于此函数，保证大小写/全半角/标点差异不产生假阴性。
    """
    t = unicodedata.normalize("NFKC", title).lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


class Item(BaseModel):
    """统一条目。

    去重四键：doi / arxiv_id / url / 标题指纹。任一非空键命中即视为重复。
    """

    source_type: SourceType
    source_name: str
    title: str
    url: str
    published_at: datetime | None = None
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    venue: str | None = None
    abstract: str | None = None
    full_text_url: str | None = None
    has_full_text: bool = False
    category: Category | None = None

    # 去重键（来源层尽量填充，缺失则该键不参与去重）
    doi: str | None = None
    arxiv_id: str | None = None

    # 原始数据，仅审计用，不参与去重与评分
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def doi_norm(self) -> str | None:
        if not self.doi:
            return None
        return self.doi.strip().lower()

    @property
    def arxiv_id_norm(self) -> str | None:
        if not self.arxiv_id:
            return None
        # 归一化 arXiv ID：去版本号 vN，去前缀
        aid = self.arxiv_id.strip().lower()
        aid = aid.replace("arxiv:", "")
        aid = re.sub(r"v\d+$", "", aid)
        return aid

    @property
    def url_norm(self) -> str | None:
        if not self.url:
            return None
        u = self.url.strip()
        # 去末尾斜杠与常见跟踪参数
        u = re.sub(r"[?&](utm_[^&]+|ref|from)=.*$", "", u)
        return u.rstrip("/")

    @property
    def title_norm(self) -> str:
        return normalize_title(self.title)

    def dedup_keys(self) -> dict[str, str | None]:
        """返回参与去重的四个键（均归一化）。None 表示该键缺失，不参与匹配。"""
        return {
            "doi": self.doi_norm,
            "arxiv_id": self.arxiv_id_norm,
            "url": self.url_norm,
            "title": self.title_norm,
        }

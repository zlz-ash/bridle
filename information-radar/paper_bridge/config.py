"""配置加载：sources.yaml + 环境变量。

来源层只消费 SourcesConfig，不直接读文件，便于测试注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时设置（从 .env 加载）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 网络
    http_proxy: str | None = "http://127.0.0.1:7890"
    https_proxy: str | None = "http://127.0.0.1:7890"
    no_proxy: str = "localhost,127.0.0.1"

    # 服务地址（Compose 内部）
    rsshub_url: str = "http://rsshub:1200"
    wewe_rss_url: str = "http://wechat-mp-rss:4000"

    # B站 / 公众号
    bilibili_uids: str = ""
    wewe_rss_token: str = ""

    # 行为
    notify_on_empty: bool = True
    failure_alert_days: int = 2

    # 请求超时与重试
    request_timeout: float = 30.0
    request_retries: int = 3


class BlogSource(BaseModel):
    name: str
    url: str
    category: str = "engineering"


class BilibiliSource(BaseModel):
    name: str
    uid: str
    category: str = "tech"


class WechatMpSource(BaseModel):
    name: str
    feed_id: str
    category: str = "tech"


@dataclass
class SourcesConfig:
    blogs: list[BlogSource] = field(default_factory=list)
    bilibili: list[BilibiliSource] = field(default_factory=list)
    wechat_mp: list[WechatMpSource] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def load_sources_config(path: str | Path = "config/sources.yaml") -> SourcesConfig:
    p = Path(path)
    if not p.exists():
        return SourcesConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    blogs = [BlogSource(**b) for b in data.get("blogs", [])]
    bilibili = [BilibiliSource(**b) for b in data.get("bilibili", [])]
    wechat_mp = [WechatMpSource(**w) for w in data.get("wechat_mp", [])]
    return SourcesConfig(blogs=blogs, bilibili=bilibili, wechat_mp=wechat_mp, raw=data)

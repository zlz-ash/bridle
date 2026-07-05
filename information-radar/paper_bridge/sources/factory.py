"""来源采集器工厂：根据 SourcesConfig 构造全部 RSS 来源实例。"""
from __future__ import annotations

from paper_bridge.config import Settings, SourcesConfig
from paper_bridge.sources.base import RSSSource
from paper_bridge.sources.bilibili import BilibiliSource
from paper_bridge.sources.blog import BlogRSSSource
from paper_bridge.sources.wechat_mp import WechatMpSource


def build_rss_sources(cfg: SourcesConfig, settings: Settings) -> list[RSSSource]:
    """根据配置构造所有 RSS 来源实例。"""
    sources: list[RSSSource] = []
    for b in cfg.blogs:
        sources.append(BlogRSSSource(name=b.name, url=b.url, category=b.category))
    for b in cfg.bilibili:
        sources.append(
            BilibiliSource(
                name=b.name, uid=b.uid, rsshub_url=settings.rsshub_url, category=b.category
            )
        )
    for w in cfg.wechat_mp:
        sources.append(
            WechatMpSource(
                name=w.name, feed_id=w.feed_id, wewe_rss_url=settings.wewe_rss_url, category=w.category
            )
        )
    return sources

"""飞书机器人推送：幂等，成功后才标记。

业务契约（来自方案）：
- 每日发送一次正式通知
- 重复执行不得重复发送（RunStore.is_pushed 拦截）
- 无新增时发送简短运行状态（RADAR_NOTIFY_ON_EMPTY 控制）
- 连续两天失败发送故障通知（独立 webhook）
"""
from __future__ import annotations

import httpx
from loguru import logger

from paper_bridge.report.brief import Brief
from paper_bridge.storage.watermark import RunStore


class FeishuNotifier:
    """飞书自定义机器人推送。

    使用 interactive 卡片消息，支持富文本。
    幂等：通过 RunStore.is_pushed 判断是否已推送，避免重发。
    """

    def __init__(
        self,
        webhook: str,
        alert_webhook: str | None = None,
        run_store: RunStore | None = None,
        proxy: str | None = "http://127.0.0.1:7890",
    ):
        self.webhook = webhook
        self.alert_webhook = alert_webhook or webhook
        self.run_store = run_store
        self._client: httpx.Client | None = None
        self._proxy = proxy

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=30.0,
                proxy=self._proxy,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    def _send(self, url: str, payload: dict) -> bool:
        """发送 HTTP POST。返回是否成功（HTTP 200 + code=0）。"""
        try:
            client = self._get_client()
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", 0) != 0:
                logger.error("feishu api error: code={} msg={}", data.get("code"), data.get("msg"))
                return False
            return True
        except Exception as e:
            logger.error("feishu send failed: {}", e)
            return False

    def push_brief(self, brief: Brief, run_id: str) -> bool:
        """推送每日简报。幂等：已推送则跳过。

        Returns:
            True 表示本次推送成功（或已推送过）。False 表示推送失败。
        """
        # 幂等检查：已推送则跳过
        if self.run_store and self.run_store.is_pushed(run_id):
            logger.info("run {} already pushed, skipping (idempotent)", run_id)
            return True

        selected = brief.selected()
        archived = brief.archived()

        if not selected and not archived:
            # 无新增：发送简短状态（由 RADAR_NOTIFY_ON_EMPTY 控制）
            return self._push_empty_status(brief, run_id)

        # 构造卡片消息
        payload = self._build_brief_card(brief)
        ok = self._send(self.webhook, payload)

        if ok and self.run_store:
            self.run_store.mark_pushed(run_id, channel="feishu", items_count=len(selected) + len(archived))
            logger.info("brief pushed: run={} items={}", run_id, len(selected) + len(archived))

        return ok

    def _build_brief_card(self, brief: Brief) -> dict:
        """构造飞书互动卡片消息。"""
        selected = brief.selected()
        archived = brief.archived()

        # 卡片标题
        title = f"📡 信息雷达每日简报 {brief.date}"
        if selected:
            title += f"（精选 {len(selected)} 篇）"

        # 卡片内容元素
        elements: list[dict] = []

        # 统计行
        if brief.stats:
            stats_text = (
                f"输入 {brief.stats.get('items_in', 0)} | "
                f"去重 {brief.stats.get('items_dedup', 0)} | "
                f"过滤 {brief.stats.get('items_filtered', 0)} | "
                f"入选 {brief.stats.get('items_selected', 0)}"
            )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**📊 统计**\n{stats_text}"},
            })

        # 精选内容
        if selected:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**⭐ 每日精选（{len(selected)} 篇）**"},
            })
            for i, item in enumerate(selected[:10], 1):  # 卡片限制，最多 10 篇
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": self._item_to_lark_md(i, item)},
                })

        # 归档摘要
        if archived:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**📁 归档备查（{len(archived)} 篇）**"},
            })
            for i, item in enumerate(archived[:5], 1):
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": self._item_to_lark_md(i, item, brief=True)},
                })
            if len(archived) > 5:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"...及另外 {len(archived) - 5} 篇"},
                })

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": elements,
            },
        }

    def _item_to_lark_md(self, idx: int, item, brief: bool = False) -> str:
        """单条目转飞书 lark_md 格式。"""
        lines = [f"**{idx}. {item.title}**"]
        lines.append(f"评分：{item.total_score}/100 | {item.venue or '未报告'} | {item.status}")
        if not brief:
            s = item.summary
            lines.append(f"研究问题：{s.get('research_question', '未报告')}")
            lines.append(f"推荐：**{s.get('recommendation', '低优先级')}**")
        if not item.has_full_text and item.partial_marker:
            lines.append(f"⚠️ {item.partial_marker}")
        lines.append(f"[查看原文]({item.url})")
        return "\n".join(lines)

    def _push_empty_status(self, brief: Brief, run_id: str) -> bool:
        """无新增时发送简短状态。"""
        payload = {
            "msg_type": "text",
            "content": {
                "text": f"📡 信息雷达 {brief.date}\n今日无新增精选内容。\n"
                        f"运行 ID：{run_id}"
            },
        }
        ok = self._send(self.webhook, payload)
        if ok and self.run_store:
            self.run_store.mark_pushed(run_id, channel="feishu", items_count=0)
            logger.info("empty status pushed: run={}", run_id)
        return ok

    def push_alert(self, message: str) -> bool:
        """发送故障通知（独立 webhook）。"""
        payload = {
            "msg_type": "text",
            "content": {"text": f"🚨 信息雷达故障告警\n{message}"},
        }
        return self._send(self.alert_webhook, payload)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

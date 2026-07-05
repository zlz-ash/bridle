"""管线编排：串联采集 → 去重 → 过滤 → 评分 → 摘要 → 简报 → 推送 → 归档。

业务契约（来自方案）：
- 增量采集：从上次成功水位之后的新内容
- 单一来源失败不影响其他来源
- 成功后才提交来源和推送水位
- 连续两天失败发送故障通知
- 无新增时也发送简短运行状态
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from paper_bridge.archive.freshrss import FreshRSSArchiver
from paper_bridge.archive.zotero import ZoteroArchiver
from paper_bridge.config import Settings, load_sources_config
from paper_bridge.http_client import build_client
from paper_bridge.logging_config import setup_logging
from paper_bridge.models import Item
from paper_bridge.notify.feishu import FeishuNotifier
from paper_bridge.pipeline.dedupe import dedupe_batch
from paper_bridge.pipeline.filter import filter_items, load_exclude_rules
from paper_bridge.pipeline.scoring import load_scoring_config, score_batch
from paper_bridge.pipeline.summarize import PaperSummary, Summarizer
from paper_bridge.report.brief import build_brief, save_brief
from paper_bridge.sources.factory import build_rss_sources
from paper_bridge.sources.papers_factory import build_paper_sources
from paper_bridge.storage.db import connect, insert_item
from paper_bridge.storage.watermark import RunStore, WatermarkStore


class Pipeline:
    """每日运行管线。

    编排顺序：
    1. 采集（各来源，失败隔离）
    2. 批内去重
    3. 规则过滤
    4. 评分
    5. AI 摘要（仅 selected + archived）
    6. 生成简报
    7. 推送（幂等）
    8. 归档（FreshRSS + Zotero）
    9. 提交水位（仅成功后）
    """

    def __init__(
        self,
        settings: Settings,
        sources_config_path: str = "config/sources.yaml",
        scoring_config_path: str = "config/scoring.yaml",
        db_path: str = "data/radar.db",
        log_dir: str = "logs",
        report_dir: str = "data/reports",
    ):
        self.settings = settings
        self.sources_config = load_sources_config(sources_config_path)
        self.scoring_config = load_scoring_config(scoring_config_path)
        self.exclude_rules = load_exclude_rules(sources_config_path)
        self.db_path = db_path
        self.report_dir = Path(report_dir)

        # 连接数据库
        self.conn = connect(db_path)
        self.watermarks = WatermarkStore(self.conn)
        self.runs = RunStore(self.conn)

        self.report_dir.mkdir(parents=True, exist_ok=True)

    def run(self, run_id: str | None = None) -> dict:
        """执行一次完整管线运行。

        Returns:
            运行统计字典。
        """
        run_id = run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        started_at = datetime.now(UTC)
        setup_logging(log_dir="logs", run_id=run_id)

        logger.info("=" * 60)
        logger.info("pipeline run started: id={}", run_id)
        logger.info("=" * 60)

        self.runs.start_run(run_id, started_at)
        stats = {
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "items_in": 0,
            "items_dedup": 0,
            "items_filtered": 0,
            "items_scored": 0,
            "items_selected": 0,
            "items_archived_tier": 0,
            "pushed": 0,
            "cost_usd": 0.0,
            "errors": [],
            "source_results": {},
        }

        try:
            # 1. 采集
            all_items = self._collect(stats)
            stats["items_in"] = len(all_items)

            # 2. 批内去重
            kept, dropped = dedupe_batch(all_items)
            stats["items_dedup"] = len(dropped)

            # 3. 跨批次去重 + 入库
            new_items = self._persist_new(kept)

            # 4. 规则过滤
            filtered, filtered_out = filter_items(new_items, self.exclude_rules)
            stats["items_filtered"] = len(filtered_out)

            # 5. 评分
            scored = score_batch(filtered, self.scoring_config)
            stats["items_scored"] = len(scored)
            stats["items_selected"] = sum(1 for _, s in scored if s.tier == "selected")
            stats["items_archived_tier"] = sum(1 for _, s in scored if s.tier == "archived")

            # 6. AI 摘要（仅 selected + archived）
            summaries = self._summarize(scored, stats)

            # 7. 生成简报
            brief = build_brief(scored, summaries, run_id, stats)
            brief_paths = save_brief(brief, self.report_dir)
            logger.info("brief saved to: {}", brief_paths)

            # 8. 推送（幂等）
            pushed = self._push(brief, run_id, stats)
            stats["pushed"] = 1 if pushed else 0

            # 9. 归档
            self._archive(brief, stats)

            # 10. 提交水位（仅成功后）
            if pushed:
                self._commit_watermarks(stats)

            finished_at = datetime.now(UTC)
            stats["finished_at"] = finished_at.isoformat()
            stats["duration_seconds"] = (finished_at - started_at).total_seconds()

            status = "ok" if pushed else "partial"
            self.runs.finish_run(run_id, finished_at, status, stats)
            logger.info("pipeline run finished: id={} status={} duration={:.1f}s",
                        run_id, status, stats["duration_seconds"])

        except Exception as e:
            logger.exception("pipeline run failed: {}", e)
            stats["errors"].append(str(e))
            finished_at = datetime.now(UTC)
            self.runs.finish_run(run_id, finished_at, "failed", stats)
            # 故障通知
            self._alert_failure(str(e), run_id)
            raise

        return stats

    def _collect(self, stats: dict) -> list[Item]:
        """采集所有来源，单来源失败隔离。"""
        all_items: list[Item] = []
        client = build_client(
            proxy=self.settings.http_proxy,
            no_proxy=self.settings.no_proxy,
            timeout=self.settings.request_timeout,
        )

        # RSS 来源
        rss_sources = build_rss_sources(self.sources_config, self.settings)
        # 论文来源
        paper_sources = build_paper_sources(self.sources_config.raw.get("papers", {}))

        all_sources = [("rss", s) for s in rss_sources] + [("paper", s) for s in paper_sources]

        for _, source in all_sources:
            source_name = getattr(source, "name", str(source))
            t0 = time.time()
            try:
                items = list(source.fetch(client))
                elapsed = time.time() - t0
                all_items.extend(items)
                stats["source_results"][source_name] = {
                    "status": "ok",
                    "items": len(items),
                    "elapsed": round(elapsed, 2),
                }
                logger.info("collected: {} items={} elapsed={:.2f}s", source_name, len(items), elapsed)
            except Exception as e:
                elapsed = time.time() - t0
                stats["source_results"][source_name] = {
                    "status": "failed",
                    "error": str(e),
                    "elapsed": round(elapsed, 2),
                }
                stats["errors"].append(f"{source_name}: {e}")
                logger.warning("source failed (isolated): {} -> {}", source_name, e)

        client.close()
        return all_items

    def _persist_new(self, items: list[Item]) -> list[Item]:
        """跨批次去重 + 入库，返回新增条目。"""
        new_items: list[Item] = []
        for item in items:
            item_id = insert_item(self.conn, item)
            if item_id is not None:
                new_items.append(item)
        logger.info("persist: total={} new={}", len(items), len(new_items))
        return new_items

    def _summarize(self, scored: list[tuple[Item, object]], stats: dict) -> dict:
        """对 selected + archived 条目生成 AI 摘要。"""
        import os

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key or api_key == "sk-replace-me":
            logger.warning("OPENAI_API_KEY not configured, skipping AI summaries")
            return {}

        summarizer = Summarizer(
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("RADAR_MODEL", "gpt-4o-mini"),
            max_tokens=int(os.environ.get("RADAR_SUMMARY_MAX_TOKENS", "1200")),
            cost_limit_usd=float(os.environ.get("RADAR_COST_LIMIT_USD", "2.0")),
        )

        summaries: dict[int, PaperSummary] = {}
        for idx, (item, score) in enumerate(scored):
            if score.tier in ("selected", "archived"):
                summaries[idx] = summarizer.summarize(item)

        stats["cost_usd"] = round(summarizer.total_cost_usd, 4)
        stats["ai_call_count"] = summarizer.call_count
        stats["ai_total_tokens"] = summarizer.total_tokens
        logger.info("AI summaries: {} calls, ${:.4f}, {} tokens",
                    summarizer.call_count, summarizer.total_cost_usd, summarizer.total_tokens)
        return summaries

    def _push(self, brief, run_id: str, stats: dict) -> bool:
        """推送简报。"""
        import os

        webhook = os.environ.get("FEISHU_WEBHOOK", "")
        if not webhook or "replace-me" in webhook:
            logger.warning("FEISHU_WEBHOOK not configured, skipping push")
            return False

        alert_webhook = os.environ.get("FEISHU_ALERT_WEBHOOK", "") or None
        notifier = FeishuNotifier(
            webhook=webhook,
            alert_webhook=alert_webhook,
            run_store=self.runs,
            proxy=self.settings.http_proxy,
        )
        ok = notifier.push_brief(brief, run_id)
        notifier.close()
        return ok

    def _archive(self, brief, stats: dict) -> None:
        """归档：普通内容进 FreshRSS，高价值论文进 Zotero。"""
        import os

        # FreshRSS
        freshrss_url = os.environ.get("FRESHRSS_URL", "http://freshrss:80")
        freshrss_user = os.environ.get("FRESHRSS_API_USER", "admin")
        freshrss_pw = os.environ.get("FRESHRSS_API_PASSWORD", "")
        if freshrss_pw and "replace-me" not in freshrss_pw:
            archiver = FreshRSSArchiver(
                base_url=freshrss_url,
                api_user=freshrss_user,
                api_password=freshrss_pw,
            )
            archived_items = brief.archived()
            ok, fail = archiver.archive_batch(archived_items)
            stats["freshrss_archived"] = ok
            stats["freshrss_failed"] = fail
            archiver.close()
        else:
            logger.info("FreshRSS not configured, skipping archive")

        # Zotero
        zotero_user = os.environ.get("ZOTERO_USER_ID", "")
        zotero_key = os.environ.get("ZOTERO_API_KEY", "")
        zotero_col = os.environ.get("ZOTERO_COLLECTION_ID", "")
        if zotero_user and zotero_key and "replace-me" not in zotero_key:
            archiver = ZoteroArchiver(
                user_id=zotero_user,
                api_key=zotero_key,
                collection_id=zotero_col,
                proxy=self.settings.http_proxy,
            )
            selected_items = brief.selected()
            ok, fail = archiver.archive_batch(selected_items)
            stats["zotero_archived"] = ok
            stats["zotero_failed"] = fail
            archiver.close()
        else:
            logger.info("Zotero not configured, skipping archive")

    def _commit_watermarks(self, stats: dict) -> None:
        """提交各来源水位（仅成功后）。"""
        now = datetime.now(UTC)
        for source_name, result in stats.get("source_results", {}).items():
            if result.get("status") == "ok":
                self.watermarks.commit_if_newer(source_name, now)
        logger.info("watermarks committed for successful sources")

    def _alert_failure(self, message: str, run_id: str) -> None:
        """发送故障通知。"""
        import os

        webhook = os.environ.get("FEISHU_ALERT_WEBHOOK", "") or os.environ.get("FEISHU_WEBHOOK", "")
        if not webhook or "replace-me" in webhook:
            logger.warning("no alert webhook configured, cannot send failure alert")
            return
        notifier = FeishuNotifier(webhook=webhook, proxy=self.settings.http_proxy)
        notifier.push_alert(f"运行 {run_id} 失败：{message}")
        notifier.close()

    def health_check(self) -> dict:
        """健康检查：各组件可用性。"""
        result = {
            "timestamp": datetime.now(UTC).isoformat(),
            "db": "ok" if self.conn else "fail",
            "sources": {},
        }

        # 检查各来源水位
        for name in self.watermarks._get_all_names():
            wm = self.watermarks.get(name)
            result["sources"][name] = {
                "last_success": wm.isoformat() if wm else None,
                "stale_days": (datetime.now(UTC) - wm).days if wm else None,
            }

        return result

    def close(self) -> None:
        self.conn.close()

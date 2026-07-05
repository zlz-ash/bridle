"""CLI 入口：paper-bridge run / health。

用法：
  python -m paper_bridge.cli run       # 执行一次完整管线
  python -m paper_bridge.cli health    # 健康检查
"""
from __future__ import annotations

import json
import sys

from paper_bridge.config import Settings


def main():
    """CLI 主入口。"""
    if len(sys.argv) < 2:
        print("用法: python -m paper_bridge.cli <run|health>")
        sys.exit(1)

    command = sys.argv[1]
    settings = Settings()

    if command == "run":
        _run_pipeline(settings)
    elif command == "health":
        _health_check(settings)
    else:
        print(f"未知命令: {command}")
        print("用法: python -m paper_bridge.cli <run|health>")
        sys.exit(1)


def _run_pipeline(settings: Settings):
    """执行完整管线。"""
    from paper_bridge.pipeline.orchestrator import Pipeline

    pipeline = Pipeline(
        settings=settings,
        sources_config_path="config/sources.yaml",
        scoring_config_path="config/scoring.yaml",
        db_path="data/radar.db",
        log_dir="logs",
        report_dir="data/reports",
    )
    try:
        stats = pipeline.run()
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    finally:
        pipeline.close()


def _health_check(settings: Settings):
    """健康检查。"""
    from paper_bridge.pipeline.orchestrator import Pipeline

    pipeline = Pipeline(
        settings=settings,
        db_path="data/radar.db",
    )
    try:
        result = pipeline.health_check()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        pipeline.close()


if __name__ == "__main__":
    main()

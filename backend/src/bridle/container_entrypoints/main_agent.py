"""Main-agent container entrypoint."""
from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

from bridle.logging.jsonl import JSONLFormatter


def configure_main_agent_logging(*, stream: TextIO | None = None) -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(getattr(handler, "formatter", None), JSONLFormatter):
            return
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JSONLFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> None:
    configure_main_agent_logging()
    if os.environ.get("BRIDLE_CONTAINER_ENTRYPOINT_STUB") == "1":
        sys.exit(0)
    from bridle.container_entrypoints.main_agent_loop import MainAgentLoop
    from bridle.container_entrypoints.main_agent_config import MainAgentConfig

    cfg = MainAgentConfig.from_env()
    loop = MainAgentLoop.from_config(cfg)
    loop.install_signal_handlers()
    sys.exit(loop.run_forever())


if __name__ == "__main__":
    main()

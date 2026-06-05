"""Node-agent container entrypoint."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


def _write_failure_manifest(outputs_dir: Path, exc: BaseException) -> None:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "failed",
        "summary": str(exc)[:500],
        "error_code": type(exc).__name__,
    }
    (outputs_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    if os.environ.get("BRIDLE_CONTAINER_ENTRYPOINT_STUB") == "1":
        sys.exit(0)

    run_id = os.environ["BRIDLE_RUN_ID"]
    node_id = os.environ["BRIDLE_NODE_ID"]
    mount = Path("/container")
    outputs_dir = mount / "output"
    try:
        from bridle.container_entrypoints.node_agent_inputs import NodeAgentInputs
        from bridle.container_entrypoints.node_agent_runner import ContainerNodeAgentRunner

        inputs = NodeAgentInputs.from_dir(mount / "inputs")
        runner = ContainerNodeAgentRunner(
            inputs=inputs,
            workspace_write_root=mount / "workspace" / "write",
            outputs_dir=outputs_dir,
            run_id=run_id,
            node_id=node_id,
        )
        manifest = asyncio.run(runner.execute())
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sys.exit(0 if manifest.get("status") != "failed" else 1)
    except Exception as exc:
        _write_failure_manifest(outputs_dir, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

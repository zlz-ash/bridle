"""Bridle CLI entry point."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

app = typer.Typer(name="bridle", help="Persistence-first AI Coding workflow kernel")

task_app = typer.Typer(name="task", help="Manage tasks")
app.add_typer(task_app, name="task")


@app.command()
def version() -> None:
    """Show version."""
    from bridle import __version__

    typer.echo(f"bridle {__version__}")


@app.command()
def serve(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory (required)",
        exists=True, file_okay=False, resolve_path=True,
    ),
    host: str = "0.0.0.0",
    port: int = 8900,
) -> None:
    """Start the API server anchored to a workspace."""
    from bridle.config import set_workspace

    set_workspace(workspace)
    typer.echo(f"Workspace: {workspace}")

    import uvicorn

    uvicorn.run("bridle.app:create_app", host=host, port=port, reload=True, factory=True)


# --- Task commands ---


@task_app.command("create")
def task_create(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
    title: str = typer.Option(..., prompt=True, help="Task title"),
    goal: str = typer.Option("", help="Task goal"),
) -> None:
    """Create a new task."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session
    from bridle.schemas.task import TaskCreateSchema
    from bridle.services.task_service import TaskService

    async def _run() -> None:
        async with async_session() as db:
            data = TaskCreateSchema(title=title, goal=goal or None)
            task = await TaskService.create(db, data)
            typer.echo(json.dumps(task.model_dump(), default=str, indent=2))

    asyncio.run(_run())


@task_app.command("list")
def task_list(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
) -> None:
    """List all tasks."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session
    from bridle.services.task_service import TaskService

    async def _run() -> None:
        async with async_session() as db:
            tasks = await TaskService.list_all(db)
            for t in tasks:
                typer.echo(f"  {t.id[:8]}  {t.status:15}  {t.title}")

    asyncio.run(_run())


@task_app.command("show")
def task_show(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
    task_id: str = typer.Argument(..., help="Task ID"),
) -> None:
    """Show task details."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session
    from bridle.services.task_service import TaskService

    async def _run() -> None:
        async with async_session() as db:
            task = await TaskService.get_by_id(db, task_id)
            if task is None:
                typer.echo(f"Task {task_id} not found", err=True)
                raise typer.Exit(1)
            typer.echo(json.dumps(task.model_dump(), default=str, indent=2))

    asyncio.run(_run())


# --- Plan commands ---

plan_app = typer.Typer(name="plan", help="Manage plans")
app.add_typer(plan_app, name="plan")


@plan_app.command("import")
def plan_import(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
    task_id: str = typer.Argument(..., help="Task ID to import plan into"),
    file: Path = typer.Argument(..., help="Path to plan JSON file", exists=True),
) -> None:
    """Import a plan from a JSON file as the global current plan."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session
    from bridle.schemas.plan import PlanImportSchema
    from bridle.services.plan_service import PlanService

    async def _run() -> None:
        plan_data = json.loads(file.read_text(encoding="utf-8"))
        plan_schema = PlanImportSchema(**plan_data)
        async with async_session() as db:
            result = await PlanService.import_plan(db, task_id, plan_schema)
            typer.echo(json.dumps(result, default=str, indent=2))

    asyncio.run(_run())


@plan_app.command("current")
def plan_current(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
) -> None:
    """Show the current active plan."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session
    from bridle.services.plan_service import PlanService

    async def _run() -> None:
        async with async_session() as db:
            plan = await PlanService.get_current(db)
            if plan is None:
                typer.echo("No active plan", err=True)
                raise typer.Exit(1)
            typer.echo(json.dumps(plan.model_dump(), default=str, indent=2))

    asyncio.run(_run())


# --- Node commands ---

node_app = typer.Typer(name="node", help="Manage nodes")
app.add_typer(node_app, name="node")


@node_app.command("run")
def node_run(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
    node_id: str = typer.Argument(..., help="Node ID to run"),
) -> None:
    """Execute a node (must belong to the current active plan)."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session

    async def _run() -> None:
        async with async_session() as db:
            from bridle.engine.blocker import Blocker
            from bridle.engine.collector import Collector
            from bridle.models.node import NodeRecord as NR
            from bridle.services.evidence_service import EvidenceService
            from bridle.services.node_service import NodeService
            from bridle.services.plan_service import PlanService
            from bridle.services.run_service import RunService

            from sqlalchemy import select

            current_plan = await PlanService.get_current(db)
            if current_plan is None:
                typer.echo("No active plan", err=True)
                raise typer.Exit(1)

            result = await db.execute(select(NR).where(NR.id == node_id))
            node_record = result.scalar_one_or_none()
            if node_record is None:
                typer.echo(f"Node {node_id} not found", err=True)
                raise typer.Exit(1)

            if node_record.plan_id != current_plan.id:
                typer.echo("Node does not belong to the current active plan", err=True)
                raise typer.Exit(1)

            plan_result = await db.execute(select(NR).where(NR.plan_id == node_record.plan_id))
            plan_nodes = plan_result.scalars().all()
            completed_ids = {n.id for n in plan_nodes if n.status == "completed"}

            block_result = Blocker.check(node_record, completed_ids)
            if block_result.blocked:
                typer.echo(f"BLOCKED: {block_result.reason}", err=True)
                raise typer.Exit(2)

            run = await RunService.create(db, node_id)
            node_record.status = "running"
            await db.commit()

            typer.echo(f"Running node: {node_record.title}...")
            from bridle.engine.sandbox_policy import SandboxPolicy
            from bridle.engine.sandboxed_tool_executor import (
                SandboxedToolExecutor,
                sandbox_results_to_command_results,
            )

            policy = SandboxPolicy.for_run(
                run_id=run.id,
                node_id=node_id,
                workspace_root=workspace,
                allowed_files=node_record.files if isinstance(node_record.files, list) else [],
                node_tests=node_record.tests if isinstance(node_record.tests, list) else [],
            )
            sandbox_result = await SandboxedToolExecutor(policy).run_allowed_tests(
                node_record.tests if isinstance(node_record.tests, list) else [],
            )
            cmd_results = sandbox_results_to_command_results(sandbox_result)

            last_result = cmd_results[-1] if cmd_results else {"exit_code": -1, "duration_ms": 0}
            await RunService.complete(
                db, run.id,
                exit_code=last_result["exit_code"],
                duration_ms=sum(r["duration_ms"] for r in cmd_results),
                stdout_path=last_result.get("stdout_path"),
                stderr_path=last_result.get("stderr_path"),
            )

            evidences = Collector.collect_for_node(node_record, cmd_results)
            for ev_data in evidences:
                await EvidenceService.create(
                    db, run_id=run.id, node_id=node_id,
                    evidence_type=ev_data["evidence_type"],
                    content=ev_data["content"], status=ev_data["status"],
                )

            all_passed = all(r["exit_code"] == 0 for r in cmd_results)
            has_missing = any(ev["status"] == "missing_evidence" for ev in evidences)
            if all_passed and not has_missing:
                node_record.status = "completed"
            elif has_missing:
                node_record.status = "missing_evidence"
            else:
                node_record.status = "failed"
            await db.commit()

            typer.echo(f"Status: {node_record.status}")
            typer.echo(f"Run ID: {run.id}")

    asyncio.run(_run())


@node_app.command("show")
def node_show(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory",
        exists=True, file_okay=False, resolve_path=True,
    ),
    node_id: str = typer.Argument(..., help="Node ID"),
) -> None:
    """Show node details (must belong to the current active plan)."""
    from bridle.config import set_workspace
    from bridle.database import _ensure_engine

    set_workspace(workspace)
    _ensure_engine()

    from bridle.database import async_session
    from bridle.services.node_service import NodeService

    async def _run() -> None:
        async with async_session() as db:
            node = await NodeService.get_by_id(db, node_id)
            if node is None:
                typer.echo(f"Node {node_id} not found (not in current plan or does not exist)", err=True)
                raise typer.Exit(1)
            typer.echo(json.dumps(node.model_dump(), default=str, indent=2))

    asyncio.run(_run())


if __name__ == "__main__":
    app()

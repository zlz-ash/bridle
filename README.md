# Bridle

Bridle is a persistence-first AI coding workflow kernel. It coordinates plans, tasks, node-agent runs, containerized execution, proposal validation, evidence capture, and API/UI surfaces for supervising coding workflows.

## Repository Layout

```text
backend/   FastAPI service, persistence layer, workflow engine, tests
frontend/  Vue 3 + TypeScript + Vite web UI
docs/      Product and technical specifications
```

## Backend

The backend package is defined in `backend/pyproject.toml`.

```powershell
cd backend
pytest
```

Useful commands:

```powershell
pytest tests/test_engine/test_container_orchestrator.py
python -m uvicorn bridle.api.app:app --reload
```

## Frontend

The frontend app is defined in `frontend/package.json`.

```powershell
cd frontend
npm run build
npm run dev
```

## Environment

Common environment variables:

```text
BRIDLE_AGENT_PROVIDER=fake|deepseek
BRIDLE_AGENT_API_KEY=...
BRIDLE_CONTAINER_RUNNER=fake
BRIDLE_CONTAINER_DRY_RUN=1
```

If dependency installation or external API access is needed on this machine, use the local proxy at port `7890`.

Do not commit real `.env` files, API keys, local databases, generated container workspaces, virtual environments, or frontend dependencies.

## Containers

Container lifecycle behavior lives under `backend/src/bridle/engine/`. The orchestration layer is responsible for create/start/wait/inspect/log collection, cleanup, diagnostics, and normalizing successful run-and-wait container results.

Docker-backed node containers can report `exited` after a successful `docker wait` exit code of `0`; Bridle treats that as a successful completed run rather than a health failure.

## Verification

Run these checks before publishing or opening a pull request:

```powershell
cd backend
pytest

cd ..\frontend
npm run build
```

Current verification snapshot:

```text
backend: 540 passed, 3 skipped
frontend: production build completed
```

## GitHub Publishing Notes

This workspace may not yet be initialized as a Git repository. Before the first push:

```powershell
git init
git add .
git commit -m "Initial Bridle project"
```

Check the staged files carefully before committing. The root `.gitignore` excludes local tool state, environment files, caches, generated runtime artifacts, build outputs, and dependency folders.

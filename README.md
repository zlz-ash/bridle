# Bridle

Persistence-first project-map workspace runtime. Backend uses FastAPI and local SQLite project maps; frontend uses React, TypeScript, and Vite.

## Start

### Backend

```powershell
cd backend
python -m pip install -e .
bridle serve --workspace D:\Bridle-workspace
```

Default API: `http://127.0.0.1:8900/api/v1`.

Health check:

```powershell
curl http://127.0.0.1:8900/api/v1/health
```

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8900`, so start the backend first.

Production build:

```powershell
cd frontend
npm run build
npm run preview
```

## Tests

Backend tests are co-located with source files and discovered from `backend/src/bridle`.

```powershell
cd backend
python -m pytest
```

Frontend tests:

```powershell
cd frontend
npm test -- --run
```

## Current Layout

```text
backend/src/bridle/features/   Product-facing backend capabilities.
backend/src/bridle/agent/      Agent capabilities: memory, skills, context, tools, runtime, providers, safety.
frontend/src/                  React application.
```

Detailed UI notes are in [Frontend-Design.md](Frontend-Design.md).

## Observability

Langfuse SDK is pinned to v4 (`langfuse>=4,<5`). The adapter uses `start_observation` with explicit parent handles and does not open network sockets during startup checks.

```powershell
cd backend
python -m bridle obs check
```
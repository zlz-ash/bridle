# Bridle

Persistence-first AI Coding workflow kernel · FastAPI backend + React frontend.

## 启动

### 修改 pyproject.toml 后

若改动了 `backend/pyproject.toml` 的 `[project.scripts]` 等包元数据：

1. 先 Ctrl+C 停掉正在跑的 backend（释放 `bridle.exe` 文件锁）
2. `D:\Bridle\.venv\Scripts\python.exe -m pip install -e D:\Bridle\backend`
3. 再执行 `bridle serve --workspace ...`

跳过第 2 步可能出现 `ModuleNotFoundError: No module named 'bridle'`。日常只改 `backend/src/*.py` 时不必重装，editable 模式会直接生效。

### 前置：Docker Desktop

1. 装好 Docker Desktop（Windows / Mac）或 docker engine（Linux）。
2. **建议设开机自启**：托盘鲸鱼图标 → Settings → General → ☑ Start Docker Desktop when you sign in。  
   设了之后开机约 30–60 秒 daemon 自动 ready，`bridle serve` 启动无感。

后端启动会自动检查 docker daemon 与本地镜像；缺失镜像会自动 build（首次约 3–5 分钟，之后秒过）。产出镜像：

- `bridle-main-agent:local`（主 Agent 决策循环）
- `bridle-node-agent:local`（节点 Worker + pytest）

手动 build：`powershell -File D:\Bridle\scripts\build-images.ps1`  
强制重 build：`bridle serve --workspace ... --rebuild-images`  
跳过容器检查（纯 API 开发）：`bridle serve --workspace ... --skip-image-build`

改动 `backend/src` 后需重新 build（或加 `--rebuild-images`）。真容器集成测试：

```powershell
$env:BRIDLE_RUN_DOCKER_TESTS = "1"
D:\Bridle\.venv\Scripts\python.exe -m pytest backend/tests/test_container_integration -q
```

### 1. 后端（先启）

```powershell
# 1. 装依赖（项目自带 venv，不要用系统 Python）
D:\Bridle\.venv\Scripts\python.exe -m pip install -e backend

# 2. 起服务，必须传 workspace 目录
D:\Bridle\.venv\Scripts\bridle.exe serve --workspace D:\Bridle-workspace
```

默认监听 `http://127.0.0.1:8900`，API 前缀 `/api/v1`。改端口加 `--port 8901`。

健康检查：

```powershell
curl http://127.0.0.1:8900/api/v1/health
```

### 2. 前端

```powershell
cd frontend
npm install         # 首次
npm run dev         # 起在 http://127.0.0.1:5173
```

Vite dev server 已配置把 `/api` 代理到 `http://127.0.0.1:8900`，**前端启动前请确保后端已经在跑**。

生产构建：

```powershell
npm run build       # 产物输出到 frontend/dist
npm run preview     # 本地预览构建产物
```

### 3. 跑测试

```powershell
D:\Bridle\.venv\Scripts\python.exe -m pytest backend/tests -q
```

## 目录

```
backend/    FastAPI 服务、SQLite 持久化、Agent 引擎、测试
frontend/   React + TypeScript + Vite Web UI
```

详细架构和接口文档见 [Frontend-Design.md](Frontend-Design.md)。

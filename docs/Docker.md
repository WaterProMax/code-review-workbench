# Docker 一键演示

在仓库根目录执行 `make demo`，灌入脚本化演示数据并启动工作台。浏览器打开：

**http://localhost:8080**

后端 API 同时发布在 **http://localhost:8000**（健康检查：`/api/health`）。页面上的 `/api` 由前端容器里的 nginx 反代到后端，不依赖浏览器直连 8000。这条反代关闭了响应缓冲，任务详情的事件流可以经 8080 持续输出。

## 命令

```bash
make demo   # 检查 Docker，构建镜像，灌入演示数据，启动前后端
make down   # 停止并删除容器和网络；命名卷 hw2-data 保留
```

没有 Docker、没有 Compose 插件，或守护进程未启动时，`make demo` 只打印错误并退出，不会启动其中一半服务。

| 终端原文 | 含义 |
|---|---|
| `error: docker was not found on PATH.` | 未安装 Docker，或当前 shell 找不到 `docker` |
| `error: 'docker compose' is not available.` | 缺少 Compose v2 插件 |
| `error: the Docker daemon is not running, or this user cannot access it.` | Docker 没启动，或当前用户无权访问守护进程 |

## `make demo` 的流程

1. 检查 `docker` 是否在 `PATH` 上、`docker compose` 是否可用、守护进程是否可连接。任一失败则退出。
2. 在本目录执行 `docker compose up --build -d --wait`（项目名 `hw2-workbench`）。
3. 后端容器入口先看数据卷 `/data` 里有没有标记文件 `.hw2-demo-seeded`。没有则在 `/app/backend` 执行：

   ```bash
   python -m scripts.seed_demo --data-dir /data
   ```

   这与 `backend/scripts/seed_demo.py` 文档中的用法一致（在 `backend/` 下 `uv run python -m scripts.seed_demo --data-dir …`）。种子使用脚本内的 `ScriptedLLMClient`，并把 `model_api_key` 显式置空，因此 **不需要** `HW2_MODEL_API_KEY`。
4. 种子成功后写入标记并启动 `uvicorn app.main:app --host 0.0.0.0 --port 8000`。容器内必须监听 `0.0.0.0`，nginx 才能访问；这与本机开发脚本默认的 `127.0.0.1` 不同。
5. 前端等到后端健康检查通过后，用 nginx 提供 Vite 生产构建，并把 `/api/` 反代到 `backend:8000`。宿主机 `8080` 映射容器 `80`。
6. 两个服务都健康后，命令打印上述地址。

再次执行 `make demo` 会发现标记文件并跳过灌数，直接启动已有数据。

种子脚本打印的任务链接是开发服务器地址 `http://127.0.0.1:5173/tasks/...`（写在 `seed_demo.py` 里）。Docker 演示请改用 8080：

| 任务 | 地址 |
|---|---|
| 任务列表 | http://localhost:8080/tasks |
| 首次审查通过 | http://localhost:8080/tasks/T-demo-pass |
| 修复后通过 | http://localhost:8080/tasks/T-demo-repair |
| 额度用尽待恢复 | http://localhost:8080/tasks/T-demo-wait |

## 环境变量与 `.env.example`

仓库根目录有 [`.env.example`](../.env.example)。需要真实模型时：

```bash
cp .env.example .env
# 只在本机 .env 中填写 HW2_MODEL_API_KEY，不要提交
```

`make demo` 通过 Compose 插值把下列变量传入**运行中的**后端容器，不会写入镜像层：

- `HW2_MODEL_PROVIDER`
- `HW2_MODEL_BASE_URL`
- `HW2_MODEL_NAME`
- `HW2_MODEL_API_KEY`
- `HW2_MODEL_TEMPERATURE`
- `HW2_LOG_LEVEL`

密钥留空或根本没有 `.env` 时，服务仍会启动。`/api/health` 里 `model_configured` 为 `false`：可以浏览已灌入的任务；新的模型任务会返回明确的配置错误，不会伪造成功。

演示栈**不**采用 `.env.example` 里的 `HW2_DATA_DIR=../data` 和 `HW2_HOST=127.0.0.1`。容器内固定为：

| 变量 | 演示栈中的值 |
|---|---|
| `HW2_DATA_DIR` | `/data`（命名卷 `hw2-data`） |
| `HW2_CONFIG_DIR` | `/app/config`（镜像内的 `config/`） |
| `HW2_FRONTEND_ORIGIN` | `http://localhost:8080` |
| 监听 | `0.0.0.0:8000` |

本机开发脚本仍是 `scripts/dev-backend.sh`（8000）和 `scripts/dev-frontend.sh`（5173）。跑 Docker 演示前先停掉占用 8000 和 8080 的进程。

## 清空演示数据

`make down` 不删除数据卷。要重新灌数：

```bash
make down
docker compose down -v
make demo
```

第二条里的 `-v` 会删掉卷 `hw2-data`。

## 镜像里有什么

- 后端：Python 3.12、`uv sync --frozen --all-groups`（锁定在 `backend/uv.lock`，含 pytest，供验证工具调用）。`config/`、应用源码，以及种子所需的 `backend/tests/fixtures`。
- 前端：`npm ci` 后 `npm run build`，运行阶段只有 nginx 和静态文件。
- 没有 `.env`，没有模型密钥。

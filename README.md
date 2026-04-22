# seedance-gateway

FastAPI 网关，代理 PPIO Seedance 视频生成 API，提供任务队列、状态查询、多 Provider 管理和管理后台。

## 目录结构

```
.
├── seedance-gateway/      # 主应用
│   ├── main.py            # FastAPI 入口
│   ├── worker.py          # 异步任务 worker
│   ├── seedance_client.py # 上游客户端
│   ├── task_manager.py    # Redis 任务管理
│   ├── provider_store.py  # Provider 管理
│   ├── models.py          # Pydantic 模型
│   ├── templates/         # 管理后台模板
│   ├── tests/             # pytest 测试
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── .env.example
└── .github/workflows/ci.yml
```

## 快速开始

### 1. 配置环境变量

```bash
cd seedance-gateway
cp .env.example .env
# 编辑 .env 填入 SEEDANCE_API_KEYS、GATEWAY_ACCESS_TOKEN 等
```

### 2. 使用 Docker Compose（推荐）

```bash
cd seedance-gateway
docker compose up --build -d
```

服务启动后：
- API: http://localhost:8001
- 健康检查: `curl http://localhost:8001/healthz`

### 3. 本地开发（uvicorn）

```bash
cd seedance-gateway
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 需要一个本地 Redis
# docker run -d -p 6379:6379 redis:7-alpine

uvicorn main:app --host 0.0.0.0 --port 8001 --reload
# 另开一个终端跑 worker
python worker.py
```

## 运行测试

```bash
cd seedance-gateway
pip install -r requirements.txt
pytest
```

覆盖率报告会输出到 `seedance-gateway/htmlcov/index.html`。

## 类型检查

```bash
pip install mypy
mypy seedance-gateway
```

## CI

推送到 `main` 或打开 PR 会触发 [.github/workflows/ci.yml](.github/workflows/ci.yml)：
- 运行 `pytest` + 覆盖率
- 构建 Docker 镜像（验证 Dockerfile 可用）

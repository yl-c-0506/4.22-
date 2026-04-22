# Seedance Gateway 使用、启动与部署说明

本文档面向实际部署和日常运维，内容尽量按“复制即可执行”的方式整理。

适用目录：当前项目根目录 [seedance-gateway](d:/ppio/seedance-gateway)

## 1. 项目用途

Seedance Gateway 是一个兼容 OpenAI 风格接口的转发网关，当前提供两类能力：

- 对外暴露统一接口：`/v1/chat/completions`、`/v1/video/generations`
- 支持按 Provider 定向路由：`/v1/providers/{provider_slug}/...`
- 支持后台管理页面：`/admin`
- 支持 Redis 持久化任务状态与 Provider 配置
- 支持单进程本地调试，以及 API + Worker 分离部署

## 2. 先看这个结论

当前项目有两种推荐启动方式：

1. 本地快速调试：单进程启动 API，任务模式使用 `inline`
2. 生产或准生产部署：使用 `docker compose` 启动 `redis + gateway-api + gateway-worker`

注意：

- 无论哪种模式，`REDIS_URL` 都是必填
- 管理后台登录优先使用 `ADMIN_ACCESS_TOKEN`；如果未配置，则回退使用 `GATEWAY_ACCESS_TOKEN`
- 当前无 slug 的默认入口 `/v1/chat/completions` 和 `/v1/video/generations` 仍走 `.env` 中配置的主上游
- 后台里“设为默认路由”不会接管无 slug 默认入口；如果你想明确走某个后台 Provider，请直接调用 `/v1/providers/{provider_slug}/...`

## 3. 环境要求

本地运行至少需要：

- Python 3.13
- Redis 7+
- PowerShell（Windows）

容器运行至少需要：

- Docker
- Docker Compose

Python 依赖来自 [requirements.txt](d:/ppio/seedance-gateway/requirements.txt)

## 4. 环境变量说明

参考模板文件：[.env.example](d:/ppio/seedance-gateway/.env.example)

最小可运行配置如下：

```env
# 上游 Seedance / PPIO 配置
SEEDANCE_API_KEYS=sk-your-ppio-api-key
SEEDANCE_BASE_URL=https://api.ppio.com

# 网关鉴权
GATEWAY_ACCESS_TOKEN=your-secret-token-here
GATEWAY_PUBLIC_URL=http://127.0.0.1:8001

# Redis
REDIS_URL=redis://localhost:6379/0

# 任务调度
TASK_POLL_INTERVAL=5
TASK_TIMEOUT=300
```

常见可选变量：

```env
ADMIN_ACCESS_TOKEN=your-admin-secret-token
TASK_EXECUTION_MODE=inline
TASK_QUEUE_POP_TIMEOUT=5
MAX_CONCURRENT_TASKS=20
WORKER_RESTART_DELAY=3
```

变量用途说明：

- `SEEDANCE_API_KEYS`：默认主线路使用的上游 API Key，多个值可用英文逗号分隔
- `SEEDANCE_BASE_URL`：默认主线路使用的上游地址
- `GATEWAY_ACCESS_TOKEN`：调用网关业务接口时使用的 Bearer Token；在未配置 `ADMIN_ACCESS_TOKEN` 时也会作为后台管理 token
- `GATEWAY_PUBLIC_URL`：用于拼接返回给客户端的任务状态查询 URL
- `ADMIN_ACCESS_TOKEN`：可选，单独用于 `/admin` 和 `/admin/api/*` 管理操作；未设置时会回退使用 `GATEWAY_ACCESS_TOKEN`
- `REDIS_URL`：Redis 连接地址
- `TASK_EXECUTION_MODE`：`inline` 表示 API 进程内执行，`queue` 表示交给独立 worker

## 5. 首次初始化

### 5.1 Windows PowerShell 初始化

在 PowerShell 中进入项目目录：

```powershell
Set-Location "D:\ppio\seedance-gateway"
```

创建 `.env` 文件：

```powershell
Copy-Item .env.example .env
notepad .env
```

创建虚拟环境并安装依赖：

```powershell
Set-Location "D:\ppio"
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
Set-Location ".\seedance-gateway"
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5.2 启动本地 Redis

如果本机未安装 Redis，直接使用 Docker 起一个：

```powershell
docker run -d --name seedance-redis -p 6379:6379 redis:7-alpine
```

检查 Redis 是否正常：

```powershell
docker ps
```

如果之前已经启动过该容器：

```powershell
docker start seedance-redis
```

## 6. 本地启动命令

### 6.1 快速调试模式：单进程 inline

适合本地联调、接口验证、前端管理页调试。

```powershell
Set-Location "D:\ppio"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
Set-Location ".\seedance-gateway"
$env:TASK_EXECUTION_MODE = "inline"
d:/ppio/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

说明：

- 这种模式下不需要单独启动 `worker.py`
- 但仍然需要 Redis

### 6.2 本地完整模式：API + Worker 分离

适合模拟生产部署链路。

终端 1，启动 API：

```powershell
Set-Location "D:\ppio"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
Set-Location ".\seedance-gateway"
$env:TASK_EXECUTION_MODE = "queue"
d:/ppio/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

终端 2，启动 Worker：

```powershell
Set-Location "D:\ppio"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
Set-Location ".\seedance-gateway"
d:/ppio/.venv/Scripts/python.exe worker.py
```

## 7. Docker Compose 一键部署

使用配置文件：[docker-compose.yml](d:/ppio/seedance-gateway/docker-compose.yml)

### 7.1 首次部署

```powershell
Set-Location "D:\ppio\seedance-gateway"
Copy-Item .env.example .env
notepad .env
docker compose up -d --build
```

### 7.2 查看运行状态

```powershell
docker compose ps
docker compose logs -f gateway-api
docker compose logs -f gateway-worker
docker compose logs -f redis
```

### 7.3 重启服务

```powershell
docker compose restart gateway-api
docker compose restart gateway-worker
```

### 7.4 停止服务

```powershell
docker compose down
```

### 7.5 连 Redis 数据一起清空

警告：这会删除 Provider 配置和任务状态缓存。

```powershell
docker compose down -v
```

### 7.6 重新构建并启动

```powershell
docker compose up -d --build gateway-api gateway-worker
```

## 8. 启动后验证命令

### 8.1 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:8001/healthz
```

预期返回：

```json
{"status":"ok"}
```

### 8.2 就绪检查

```powershell
Invoke-RestMethod http://127.0.0.1:8001/readyz
```

预期返回：

```json
{"status":"ready"}
```

### 8.3 管理后台

浏览器打开：

```text
http://127.0.0.1:8001/admin
```

在页面顶部输入：

```text
ADMIN_ACCESS_TOKEN（未配置时可使用 GATEWAY_ACCESS_TOKEN）
```

对应的实际值，例如：

```text
my-seedance-gateway-token
```

## 9. 后台管理页面怎么用

管理页模板文件在 [templates/admin.html](d:/ppio/seedance-gateway/templates/admin.html)

进入 `/admin` 后：

1. 先输入管理后台 Bearer Token：优先使用 `ADMIN_ACCESS_TOKEN`，未配置时回退使用 `GATEWAY_ACCESS_TOKEN`
2. 使用顶部“刷新”按钮加载现有 Provider 列表
3. 使用“项目功能菜单”执行状态检查、复制接口地址和复制运维命令
4. 在左侧表单新增 Provider
5. 在右侧列表中执行编辑、停用、设为默认、删除

### 9.1 项目功能菜单

管理页顶部新增了“项目功能菜单”，用于把常用操作直接收敛到页面里。

当前包含以下菜单：

- `状态检查`
- `接口地址`
- `密钥管理`
- `运维命令`
- `使用提示`

其中包含这些快捷按钮：

- `健康检查`
- `就绪检查`
- `复制 Chat 接口`
- `复制 Video 接口`
- `复制任务查询模板`
- `复制 Admin 地址`
- `查看已有 API Keys`
- `保存 API Keys`
- `复制本地启动命令`
- `复制 API + Worker 启动命令`
- `复制 Docker 部署命令`
- `复制查看日志命令`

这些按钮的作用：

- 直接在页面内检查 `/healthz` 与 `/readyz`
- 一键复制默认主线路接口地址
- 直接查看某个 Provider 当前已保存的真实 API Key 列表
- 在页面内完成 API Keys 的新增、删除、修改并直接保存
- 一键复制本地调试或 Docker 部署命令
- 减少在 README 和控制台之间反复切换

### 9.2 密钥管理怎么用

进入顶部 `密钥管理` 菜单后：

1. 先点击顶部 `刷新`，确保 Provider 列表和下拉框已加载
2. 在下拉框中选择目标 Provider
3. 点击 `查看已有 API Keys`
4. 等待右侧加载出当前完整列表后，再修改现有 key 或点击 `新增 Key`
5. 如需删除某一行，点击该行的 `删除`
6. 修改完成后点击 `保存 API Keys`

你也可以直接在右侧 Provider 列表里点击某个条目的 `密钥管理` 按钮，页面会自动跳到该面板并加载对应 Key。

补充说明：

- `查看已有 API Keys` 调用的是后台详情接口 `GET /admin/api/providers/{provider_slug}`
- `保存 API Keys` 调用的是更新接口 `PUT /admin/api/providers/{provider_slug}`
- 保存前必须先加载当前 Provider 的完整列表，避免误覆盖其它已有 Key
- 保存时会以当前页面上的完整列表覆盖原有 Key 列表
- 页面不再把后台 Token 保存在浏览器 `sessionStorage` 里，关闭或刷新后如需继续操作请重新输入 Token
- 建议生产环境额外设置 `ADMIN_ACCESS_TOKEN`，不要直接把业务网关 token 用作后台管理 token

### 9.3 Provider 表单和列表

字段说明：

- `Slug`：Provider 唯一标识，只允许小写字母、数字、连字符
- `Name`：显示名称
- `API Base URL`：该 Provider 的上游地址
- `API Keys`：每行一个 key
- `设为全局默认路由`：当前主要用于标记和后台元数据维护

编辑模式说明：

- 编辑已有 Provider 时，`API Keys` 可以留空
- 留空表示保持原有 key 列表不变
- 只有新增 Provider 时，`API Keys` 才是必填

重要说明：

- 当前无 slug 的默认入口不会自动切到后台“默认 Provider”
- 如果你要明确使用某个 Provider，应该直接调用 `/v1/providers/{provider_slug}/...`

## 10. 接口调用方式

### 10.1 默认主线路接口

这两个接口使用 `.env` 中的：

- `SEEDANCE_API_KEYS`
- `SEEDANCE_BASE_URL`

可用地址：

- `POST /v1/chat/completions`
- `POST /v1/video/generations`
- `GET /v1/tasks/{task_id}`

### Chat Completions 示例

```powershell
$headers = @{
	Authorization = "Bearer my-seedance-gateway-token"
	"Content-Type" = "application/json"
}

$body = @{
	model = "seedance-v1"
	messages = @(
		@{
			role = "user"
			content = "生成一个海边日落的短视频"
		}
	)
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
	-Uri "http://127.0.0.1:8001/v1/chat/completions" `
	-Method Post `
	-Headers $headers `
	-Body $body
```

### Video Generations 示例

```powershell
$headers = @{
	Authorization = "Bearer my-seedance-gateway-token"
	"Content-Type" = "application/json"
}

$body = @{
	model = "seedance-v1"
	prompt = "生成一个赛博朋克城市夜景镜头"
	duration = 5
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
	-Uri "http://127.0.0.1:8001/v1/video/generations" `
	-Method Post `
	-Headers $headers `
	-Body $body
```

### 10.2 指定 Provider 的接口

如果你要强制走后台某个具体 Provider，使用以下路由：

- `POST /v1/providers/{provider_slug}/chat/completions`
- `POST /v1/providers/{provider_slug}/video/generations`
- `GET /v1/providers/{provider_slug}/tasks/{task_id}`

示例：

```powershell
$headers = @{
	Authorization = "Bearer my-seedance-gateway-token"
	"Content-Type" = "application/json"
}

$body = @{
	model = "seedance-v1"
	prompt = "生成一段宇宙飞船穿越星云的视频"
	duration = 5
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
	-Uri "http://127.0.0.1:8001/v1/providers/demo-provider/video/generations" `
	-Method Post `
	-Headers $headers `
	-Body $body
```

### 10.3 查询任务状态

接口返回中会带任务状态 URL，优先使用返回值中的 `url` 或 `Status URL`。

如果你已有任务 ID，也可以手动查询：

```powershell
$headers = @{
	Authorization = "Bearer my-seedance-gateway-token"
}

Invoke-RestMethod `
	-Uri "http://127.0.0.1:8001/v1/tasks/task-123" `
	-Method Get `
	-Headers $headers
```

## 11. 常用运维命令

### 11.1 查看完整测试结果

```powershell
Set-Location "D:\ppio\seedance-gateway"
d:/ppio/.venv/Scripts/python.exe -m pytest -q --tb=short
```

### 11.2 只跑主入口测试

```powershell
Set-Location "D:\ppio\seedance-gateway"
d:/ppio/.venv/Scripts/python.exe -m pytest tests/test_main.py -q --tb=short
```

### 11.3 查看 HTML 覆盖率报告

测试完成后打开：

```text
d:\ppio\seedance-gateway\htmlcov\index.html
```

### 11.4 查看容器日志

```powershell
docker compose logs -f gateway-api
docker compose logs -f gateway-worker
docker compose logs -f redis
```

## 12. 常见问题

### 12.1 `/readyz` 返回 503

优先检查：

- Redis 是否启动
- `.env` 中 `REDIS_URL` 是否正确
- API 进程是否能连通 Redis

### 12.2 管理页能打开但刷新失败

优先检查：

- 输入的是否是 `ADMIN_ACCESS_TOKEN`，如果未配置该变量，再确认是否应使用 `GATEWAY_ACCESS_TOKEN`
- 是否带上了 `Bearer Token` 对应的实际值
- 后台接口是否返回 401

### 12.3 后台设了“默认路由”，为什么默认入口没切换

这是当前实现行为。

目前：

- `/v1/chat/completions`
- `/v1/video/generations`

仍然使用 `.env` 中主线路配置。

如果你要强制走后台某个 Provider，请改用：

- `/v1/providers/{provider_slug}/chat/completions`
- `/v1/providers/{provider_slug}/video/generations`

### 12.4 本地调试是否一定要启动 worker

不一定。

- 如果 `TASK_EXECUTION_MODE=inline`，不需要单独启动 worker
- 如果 `TASK_EXECUTION_MODE=queue`，必须同时启动 `worker.py`

## 13. 相关文件

- 入口服务：[main.py](d:/ppio/seedance-gateway/main.py)
- Worker：[worker.py](d:/ppio/seedance-gateway/worker.py)
- Provider 存储：[provider_store.py](d:/ppio/seedance-gateway/provider_store.py)
- 管理页模板：[templates/admin.html](d:/ppio/seedance-gateway/templates/admin.html)
- Docker 构建：[Dockerfile](d:/ppio/seedance-gateway/Dockerfile)
- Compose 部署：[docker-compose.yml](d:/ppio/seedance-gateway/docker-compose.yml)
- 环境变量模板：[.env.example](d:/ppio/seedance-gateway/.env.example)
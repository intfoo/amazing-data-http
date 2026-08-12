# AmazingData HTTP 适配服务

将 AmazingData SDK 的日 K 数据通过 HTTP 接口暴露给主项目的自定义数据源。

## 架构

```
HTTP 客户端
    │ POST /daily       (codes, start_time, end_time)
    │ POST /minute      (codes, period, start_time, end_time)
    │ POST /adj_factor  (codes, start_time, end_time)
    │ GET  /realtime    (?codes=)
    │ GET  /health
    ▼
┌─────────────────────────────────────────────────────┐
│  AmazingData HTTP 适配服务 (FastAPI)                 │
│  ├── auth.py               Bearer Token 认证守卫     │
│  ├── http_app.py           路由 + 错误处理           │
│  ├── kline_service.py      日K/分钟K 转换 + 展平     │
│  ├── adj_factor_service.py 除权因子 melt 长表        │
│  ├── realtime_service.py   订阅缓存 + fallback       │
│  ├── gateway.py            SDK 封装 (login/query)    │
│  ├── serializer.py         DataFrame → JSON          │
│  ├── errors.py             错误码 + request_id       │
│  └── health.py             健康检查                  │
└─────────────────────────────────────────────────────┘
    │ AmazingData SDK
    ▼
  tgw 原生数据服务
```

**职责边界**：适配服务保留 SDK 原始字段名（`code`、`kline_time`、`open`…），不重命名、不换算单位、不复权。字段映射、单位换算、复权计算由消费方自行处理。

### 项目结构

```
app/
├── config.py             # 环境变量配置（Config dataclass）
├── errors.py             # 错误码 + AppError + request_id 中间件
├── auth.py               # Bearer Token 认证依赖
├── http_app.py           # FastAPI 应用（/daily /minute /adj_factor /realtime /health 路由）
├── gateway.py            # Gateway 接口 + AmazingDataGateway SDK 封装
├── kline_service.py      # 日期转换 + dict[code, DataFrame] 展平
├── adj_factor_service.py # 除权因子查询（宽表 melt 长表 + dropna）
├── realtime_service.py   # 实时行情订阅缓存 + snapshot fallback
├── serializer.py         # DataFrame/NumPy/datetime → JSON 序列化
└── health.py             # 健康检查服务
scripts/
├── run.py                # 统一启动入口（本地/Docker/Podman/SDK 安装）
├── probe_sdk.py          # SDK 登录门禁探测（启动前校验凭据）
├── probe_code_info.py    # 证券基础信息探测
├── probe_index_mixed.py  # 指数成分探测
├── probe_snapshot.py     # 快照接口探测
└── probe_subscription.py # 实时订阅探测
tests/                    # 单元测试（13 个文件，107 个用例，全 FakeGateway）
docs/
└── API.md                # HTTP 接口契约
Dockerfile                # python:3.14-slim + SDK wheel + 系统库
docker-compose.yml        # 部署编排（env_file 注入 .env）
pyproject.toml            # 依赖声明
uv.lock                   # uv 锁文件
```

## 前置条件

- Docker（支持 `linux/amd64` 平台）
- AmazingData 账号凭据（用户名、密码、服务器 IP、端口）
- 本地 wheel 文件（已包含在项目根目录，按 Python 版本选用）：
  - `tgw-*-py3-none-any.whl`（tgw 原生库，纯 Python，3.13/3.14 通用）
  - `AmazingData-*-cp313-none-any.whl`（Python 3.13 用）
  - `AmazingData-*-cp314-none-any.whl`（Python 3.14 用）
- Python 运行时依赖（声明在 `pyproject.toml`）：`fastapi` / `uvicorn[standard]` / `pandas` / `numpy` / `tables`
  - 本地 `scripts/run.py` 模式 1 会自动安装（SDK wheel + 全部 Python 依赖，含 `tables`）
  - Docker 模式由 Dockerfile `pip install .` 自动安装
  - 手动安装：`pip install -e .`（读 `pyproject.toml`）或 `pip install fastapi "uvicorn[standard]" pandas numpy tables`

> `tables`（pytables）是 SDK `get_adj_factor`（复权因子 HDF5 本地缓存）的隐式依赖，SDK whl 未声明，必须单独确保安装。`/adj_factor` 端点依赖它；`/daily` `/minute` `/realtime` 不需要。

## 验证方式一：单元测试（无需 SDK、无需 Docker）

验证所有业务逻辑（日期转换、序列化、展平、路由、错误处理），全部使用 FakeGateway，不依赖真实 SDK：

```bash
pip install fastapi uvicorn pandas numpy pytest httpx
python -m pytest -v
```

预期输出：`107 passed`。

## 验证方式二：本地真实 SDK（需要 Python 3.13 或 3.14 + 凭据，无需 Docker）

一条命令完成：交互式填凭据 →（可选）装 SDK → probe 登录验证 → 起 uvicorn。

```bash
python scripts/run.py
# 选模式 1=本地
```

首次运行会逐项询问用户名 / IP / 端口 / 密码（密码隐藏输入），probe 登录验证通过后写入 `.env`（gitignored），随后每次启动自动读取并重跑 probe 门禁。

> SDK wheel 按 Python 解释器版本自动选 `cp313` / `cp314`；`import AmazingData` 失败时会询问是否自动 `pip install`。启动 uvicorn 前还会检查 `tables`（pytables，复权因子缓存依赖），缺失时提示自动安装。

启动后用以下命令手动验证：

```bash
curl http://localhost:3021/health
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```

> 本地模式和 Docker 模式均统一用 `.env` 存储凭据。

## 验证方式三：Docker 完整链路（需要 Docker + 凭据）

```bash
python scripts/run.py
# 选模式 2=Docker
```

交互式填凭据后自动生成 `.env`（若已存在会询问覆盖，旧文件备份为 `.env.bak`）；随后询问是否执行 `docker build`、容器内 probe 门禁、`docker compose up -d`，每步可选 n 改为手动执行。

启动后手动验证：

```bash
curl http://localhost:3021/health          # 期望 {"status":"ok"}
# 若 503：docker compose logs amazingdata-http 查登录错误
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```

错误场景验证（反向日期 / 空代码 / 错误格式）见 [docs/API.md](docs/API.md)。

## API 参考

接口契约详见 [docs/API.md](docs/API.md)，含 `/daily`、`/minute`、`/realtime`、`/health`、错误响应格式与错误码表。

## 配置说明

服务通过环境变量读取配置。本地模式和 Docker 模式统一使用 `.env` 文件：

| 模式 | 配置文件 | 注入方式 | 启动入口 |
|------|---------|---------|---------|
| 本地模式 | `.env`（KEY=VALUE） | `scripts/run.py` 模式 1 读取后注入 `os.environ` | `python scripts/run.py` 选 1 |
| Docker/Podman 模式 | `.env`（KEY=VALUE） | `docker-compose.yml` 的 `env_file` 注入容器 | `python scripts/run.py` 选 2/3 |

`scripts/run.py` 交互式向导只写入 4 项 SDK 凭据 + `HTTP_HOST` + `HTTP_PORT`；认证、并发限制等字段需手动编辑 `.env` 追加。`.env` 的所有键值都会被注入环境变量。

### 字段参考

| 变量 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `AMAZINGDATA_USERNAME` | 是 | — | AmazingData 账号 |
| `AMAZINGDATA_PASSWORD` | 是 | — | AmazingData 密码（不写入日志） |
| `AMAZINGDATA_HOST` | 是 | — | SDK 登录目标服务器 IP/主机名 |
| `AMAZINGDATA_PORT` | 是 | — | SDK 登录目标服务器端口 |
| `HTTP_HOST` | 否 | `0.0.0.0` | 本服务 HTTP 监听地址。容器内必须 `0.0.0.0`；仅本机访问可改 `127.0.0.1` |
| `HTTP_PORT` | 否 | `3021` | 本服务 HTTP 监听端口。Docker 模式下 `docker-compose.yml` 端口映射同步读取此变量，`Dockerfile` CMD 也读取它 |
| `SDK_MAX_CONCURRENT` | 否 | `2` | SDK 最大并发调用数（SDK 调用全局串行，此值只决定排队深度），超出返回 503 `SERVICE_BUSY` |
| `AUTH_TOKEN` | 视情况 | `""` | Bearer token。`AUTH_REQUIRED=true` 时必填，客户端需带 `Authorization: Bearer <token>`。强度要求：长度 > 12 且同时含字母和数字，弱 token 阻止启动 |
| `AUTH_REQUIRED` | 否 | `true` | 认证开关。`false` 时认证彻底关闭，所有请求直接放行，`AUTH_TOKEN` 被忽略。仅本地调试用，生产必须保持 `true` |
| `ADJ_FACTOR_LOCAL_PATH` | 否 | `""` | SDK `get_adj_factor` 的 `local_path` 参数，必须为绝对路径。留空由 SDK 自行管理 HDF5 缓存 |
| `ADJ_FACTOR_IS_LOCAL` | 否 | `false` | 复权因子是否使用本地缓存。`true` 时优先读本地 HDF5 文件，`false` 时从服务端拉取 |
| `SUBSCRIPTION_OPEN` | 否 | `09:00` | 订阅窗口开始（HH:MM）。仅在交易日窗口内启动快照订阅，非交易时段不持有订阅会话以降 CPU。SDK 查询接口不受影响 |
| `SUBSCRIPTION_CLOSE` | 否 | `15:20` | 订阅窗口结束（HH:MM） |
| `STALE_THRESHOLD_SEC` | 否 | `90` | watchdog 失活阈值（秒）。窗口期内连续 N 秒未收到快照即判定订阅失活，`/health` 返回 503 触发容器重启 |
| `WATCHDOG_INTERVAL_SEC` | 否 | `60` | watchdog 检查间隔（秒） |
| `ETF_FLOW_CACHE_TTL_SEC` | 否 | `300` | /etf/net_inflow 结果缓存 TTL（秒） |

> 四项凭据缺失时进程仍可启动，`/health` 返回 503 `config: incomplete`，便于 Docker 日志暴露诊断信息。认证配置无效（`AUTH_REQUIRED=true` 但 token 为空/过弱）时进程启动即退出。非交易时段或窗口期外不启动订阅，`/health` 报 `realtime_detail: inactive_offhours` 且仍返回 200；窗口期内订阅失活则报 503（`inactive_stale`/`inactive_error`/`inactive_not_started`）。

### 配置文件示例

`.env`（本地模式和 Docker 模式通用）：

```ini
AMAZINGDATA_USERNAME=your_account
AMAZINGDATA_PASSWORD=your_password
AMAZINGDATA_HOST=1.2.3.4
AMAZINGDATA_PORT=8600
HTTP_HOST=0.0.0.0
HTTP_PORT=3021
AUTH_TOKEN=your_token_with_letters_and_digits_123
AUTH_REQUIRED=true
# 实时订阅窗口与存活检测（可选，留空用默认值）
SUBSCRIPTION_OPEN=09:00
SUBSCRIPTION_CLOSE=15:20
STALE_THRESHOLD_SEC=90
WATCHDOG_INTERVAL_SEC=60
```

`.env` 含凭据，不应提交到版本库（`.env.example` 是可提交的脱敏模板）。

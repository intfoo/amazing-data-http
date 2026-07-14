# AmazingData HTTP 适配服务

将 AmazingData SDK 1.1.7 的日 K 数据通过 HTTP 接口暴露给主项目的自定义数据源。

## 架构

```
主项目数据同步任务
    │ POST /daily  (symbols, start_time, end_time)
    ▼
┌─────────────────────────────────────────┐
│  AmazingData HTTP 适配服务 (FastAPI)     │
│  ├── http_app.py   路由 + 错误处理       │
│  ├── kline_service 日期转换 + 展平       │
│  ├── gateway.py   SDK 封装 (login/query) │
│  ├── serializer.py DataFrame → JSON      │
│  └── health.py    健康检查               │
└─────────────────────────────────────────┘
    │ AmazingData SDK 1.1.7
    ▼
  tgw 原生数据服务
```

**职责边界**：适配服务保留 SDK 原始字段名（`code`、`kline_time`、`open`…），不重命名、不换算单位、不复权。字段映射由主项目 YAML 的 `field_map` 完成。

## 前置条件

- Docker（支持 `linux/amd64` 平台）
- AmazingData 账号凭据（用户名、密码、服务器 IP、端口）
- 本地 wheel 文件（已包含在项目根目录，按 Python 版本选用）：
  - `tgw-1.0.8.7-py3-none-any.whl`（tgw 原生库，纯 Python，3.13/3.14 通用）
  - `AmazingData-1.1.7-cp313-none-any.whl`（Python 3.13 用）
  - `AmazingData-1.1.7-cp314-none-any.whl`（Python 3.14 用）

## 验证方式一：单元测试（无需 SDK、无需 Docker）

验证所有业务逻辑（日期转换、序列化、展平、路由、错误处理），全部使用 FakeGateway，不依赖真实 SDK：

```bash
pip install fastapi uvicorn pandas numpy pytest httpx
python -m pytest -v
```

预期输出：`82 passed`。

## 验证方式二：本地真实 SDK（需要 Python 3.13 或 3.14 + 凭据，无需 Docker）

一条命令完成：交互式填凭据 →（可选）装 SDK → probe 登录验证 → 起 uvicorn。

```bash
python scripts/run.py
# 选模式 1=本地
```

首次运行会逐项询问用户名 / IP / 端口 / 密码（密码隐藏输入），probe 登录验证通过后写入 `local.config.json`（gitignored），随后每次启动自动读取并重跑 probe 门禁。

> SDK wheel 按 Python 解释器版本自动选 `cp313` / `cp314`；`import AmazingData` 失败时会询问是否自动 `pip install`。

启动后用以下命令手动验证：

```bash
curl http://localhost:3021/health
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```

> 本地模式凭据存 `local.config.json`，Docker 模式存 `.env`，二者互不读取。

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

错误场景验证（反向日期 / 空代码 / 错误格式）同 API 参考章节。

### 主项目集成配置

在主项目的自定义数据源 YAML 中配置：

```yaml
# 数据源：日 K
url: http://amazingdata-http:3021/daily
method: POST
response_path: data          # 从响应 JSON 的 data 字段提取记录数组
auth_type: none              # 内网服务，无需认证
batch: 100                   # 每次请求最多 100 个代码
rpm: 200                     # 每分钟最多 200 次请求

# 字段映射：upstream_field → internal_field
field_map:
  code: symbol               # SDK 的 code → 主项目的 symbol
  kline_time: date           # SDK 的 kline_time → 主项目的 date
  open: open
  high: high
  low: low
  close: close
  volume: volume
  amount: amount

# 日期解析：SDK 返回 ISO 格式
transforms:
  date: "parse_date(value, '%Y-%m-%d')"
```

> `field_map` 方向是 `upstream_field: internal_field`（左=适配服务返回的字段名，右=主项目内部字段名）。

在主项目中执行"试拉测试"，确认：
1. 能解析 `response_path: data` 指向的数组
2. 字段映射正确（`code→symbol`、`kline_time→date`…）
3. 日期列能被 `parse_date` 正确解析
4. 数据行数 > 0（在交易日区间内）

## API 参考

### POST /daily

查询日 K 数据。

**请求体**：
```json
{
  "symbols": ["000001.SZ", "600000.SH"],
  "start_time": "2024-01-01",
  "end_time": "2024-01-31"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `symbols` | string[] | 非空数组（必填） |
| `start_time` | string | 可选。支持 `YYYY-MM-DD`、`YYYY-MM-DDTHH:MM:SS`、`YYYYMMDD` 等 ISO 格式；时间部分截断只取日期。缺省时由 SDK 使用默认起始日 `20240101` |
| `end_time` | string | 可选。格式同上。缺省时由 SDK 使用默认结束日 `20991231`；仅当两者都提供时校验 `start_time <= end_time`（按日期比较） |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "kline_time": "...", "open": ..., ...}]}
```

**空结果**（HTTP 200）：`{"data": []}`

### GET /health

健康检查。

**正常**（HTTP 200）：
```json
{"status": "ok", "sdk": "ready", "config": "complete"}
```

**异常**（HTTP 503）：
```json
{"status": "degraded", "sdk": "not_ready", "config": "incomplete"}
```

### 错误响应格式

```json
{
  "error": {
    "code": "SDK_QUERY_FAILED",
    "message": "查询日K失败",
    "request_id": "abc-123-def"
  }
}
```

| 错误码 | HTTP | 含义 |
|--------|------|------|
| `INVALID_REQUEST` | 422 | 请求体、代码列表或日期参数无效 |
| `SDK_NOT_READY` | 503 | SDK 未登录或未初始化 |
| `SDK_QUERY_FAILED` | 502 | 上游 SDK 查询失败 |
| `SERIALIZATION_FAILED` | 502 | 返回值无法安全序列化 |
| `INTERNAL_ERROR` | 500 | 未分类的内部错误 |

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `AMAZINGDATA_USERNAME` | 是 | AmazingData 账号 |
| `AMAZINGDATA_PASSWORD` | 是 | AmazingData 密码 |
| `AMAZINGDATA_HOST` | 是 | 服务器地址（IP/主机名，SDK host 参数） |
| `AMAZINGDATA_PORT` | 是 | 服务器端口 |
| `HTTP_HOST` | 否 | 监听地址，默认 `0.0.0.0` |
| `HTTP_PORT` | 否 | 监听端口，默认 `3021` |

> 本地模式用 `local.config.json`，Docker 模式用 `.env`，二者互不读取。

## 项目结构

```
app/
├── config.py        # 环境变量配置
├── serializer.py    # DataFrame/NumPy/datetime → JSON 序列化
├── gateway.py       # Gateway 接口 + AmazingDataGateway SDK 封装
├── kline_service.py # 日期转换 + dict[code, DataFrame] 展平
├── health.py        # 健康检查服务
├── errors.py        # 错误码 + AppError + request_id 中间件
└── http_app.py      # FastAPI 应用（/daily + /health 路由）
scripts/
└── probe_sdk.py     # SDK 探测脚本（spec §5.2 门禁）
tests/               # 单元测试（82 个）
Dockerfile           # python:3.14 + SDK wheel
docker-compose.yml   # 部署编排
```

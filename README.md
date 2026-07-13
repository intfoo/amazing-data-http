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

**职责边界**：适配服务保留 SDK 原始字段名（`code`、`trade_time`、`open`…），不重命名、不换算单位、不复权。字段映射由主项目 YAML 的 `field_map` 完成。

## 前置条件

- Docker（支持 `linux/amd64` 平台）
- AmazingData 账号凭据（用户名、密码、服务器 IP、端口）
- 两个本地 wheel 文件（已包含在项目根目录）：
  - `tgw-1.0.8.7-py3-none-any.whl`
  - `AmazingData-1.1.7-cp314-none-any.whl`

## 快速验证（无需 SDK 凭据）

本地无 Docker / 无凭据时，可先验证代码逻辑（单元测试，45 个用例）：

```bash
pip install fastapi uvicorn pandas numpy pytest httpx
python -m pytest -v
```

预期输出：`45 passed`。

## 完整链路验证（需要凭据 + Docker）

### 第 1 步：配置凭据

创建 `.env` 文件（**不要提交到 git**）：

```bash
cp .env.example .env
# 编辑 .env，填入真实凭据
```

```text
AMAZINGDATA_USERNAME=你的账号
AMAZINGDATA_PASSWORD=你的密码
AMAZINGDATA_IP=服务器IP
AMAZINGDATA_PORT=服务器端口
HTTP_HOST=0.0.0.0
HTTP_PORT=3021
```

### 第 2 步：构建 Docker 镜像

```bash
docker build --platform linux/amd64 -t amazingdata-http:probe .
```

**如果构建失败**：`numba`/`scipy` 可能没有 Python 3.14 的 wheel。这是 spec §5.1 的已知风险，需要补充兼容 wheel 或退回兼容 SDK 版本。

### 第 3 步：SDK 探测（spec §5.2 强制门禁）

在正式启动服务前，先运行探测脚本，确认 SDK API 与代码假设一致：

```bash
docker run --rm --env-file .env --platform linux/amd64 amazingdata-http:probe python scripts/probe_sdk.py > docs/probe-report.json
```

打开 `docs/probe-report.json`，确认以下关键字段：

| 字段 | 预期值 | 若不一致 |
|------|--------|----------|
| `login_ok` | `true` | 检查凭据和网络 |
| `login_params.port` | 参数名 `port` | 若为 `host`，修改 `app/gateway.py` 的 `ad.login(...)` 调用 |
| `query_ok` | `true` | 检查 SDK 查询参数 |
| `df_columns` | 含 `code, trade_time, open, high, low, close, volume, amount` | 调整主项目 `field_map` |
| `df_index_name` | 索引名（如 `trade_time`） | 影响 `serialize_dataframe` 的索引重置行为 |
| `period_values.day` | 整数值 | 确认 `Period.day.value` 可正常获取 |

### 第 4 步：启动服务

```bash
docker compose up -d
```

查看启动日志，确认登录成功：

```bash
docker compose logs amazingdata-http | Select-String "login"
```

预期看到：`gateway login succeeded on startup`

### 第 5 步：验证健康检查

```bash
curl http://localhost:3021/health
```

**预期**（服务正常）：
```json
{"status":"ok","sdk":"ready","config":"complete"}
```
HTTP 状态码 `200`。

**若返回 503**：检查 `.env` 凭据是否正确、SDK 是否能连接服务器。响应不含密码，可安全查看。

### 第 6 步：验证日 K 查询

```bash
curl -X POST http://localhost:3021/daily ^
  -H "Content-Type: application/json" ^
  -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```

**预期**（有数据）：
```json
{
  "data": [
    {
      "code": "000001.SZ",
      "trade_time": "2024-01-02T00:00:00",
      "open": 10.2,
      "high": 10.45,
      "low": 10.1,
      "close": 10.3,
      "volume": 1234567,
      "amount": 12700000.0
    },
    ...
  ]
}
```

> 字段名以 `docs/probe-report.json` 的 `df_columns` 为准。

**验证空结果**（用一个无交易的日期）：
```bash
curl -X POST http://localhost:3021/daily ^
  -H "Content-Type: application/json" ^
  -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-02-10\",\"end_time\":\"2024-02-10\"}"
```

预期：HTTP `200`，`{"data": []}`（空结果不是错误）。

### 第 7 步：验证错误场景

**反向日期**（start > end）：
```bash
curl -X POST http://localhost:3021/daily ^
  -H "Content-Type: application/json" ^
  -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-12-31\",\"end_time\":\"2024-01-01\"}"
```
预期：HTTP `422`，`{"error":{"code":"INVALID_REQUEST",...}}`

**空代码列表**：
```bash
curl -X POST http://localhost:3021/daily ^
  -H "Content-Type: application/json" ^
  -d "{\"symbols\":[],\"start_time\":\"2024-01-01\",\"end_time\":\"2024-01-31\"}"
```
预期：HTTP `422`

**错误日期格式**：
```bash
curl -X POST http://localhost:3021/daily ^
  -H "Content-Type: application/json" ^
  -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"20240101\",\"end_time\":\"2024-01-31\"}"
```
预期：HTTP `422`

每个错误响应都包含 `request_id`，可在 `docker compose logs` 中搜索该 ID 定位完整上下文。

### 第 8 步：主项目集成验证

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
  trade_time: date           # SDK 的 trade_time → 主项目的 date
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
2. 字段映射正确（`code→symbol`、`trade_time→date`…）
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
| `symbols` | string[] | 非空数组 |
| `start_time` | string | `YYYY-MM-DD` 格式 |
| `end_time` | string | `YYYY-MM-DD` 格式，不早于 `start_time` |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "trade_time": "...", "open": ..., ...}]}
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
| `AMAZINGDATA_IP` | 是 | 服务器 IP |
| `AMAZINGDATA_PORT` | 是 | 服务器端口 |
| `HTTP_HOST` | 否 | 监听地址，默认 `0.0.0.0` |
| `HTTP_PORT` | 否 | 监听端口，默认 `3021` |

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
tests/               # 45 个单元测试
Dockerfile           # python:3.14 + SDK wheel
docker-compose.yml   # 部署编排
```

# API 参考

> 本文件从 README 抽离，只描述 HTTP 接口契约。部署、启动、配置等内容见 [README](../README.md)。

## POST /daily

查询日 K 数据。

**请求体**：
```json
{
  "codes": ["000001.SZ", "600000.SH"],
  "start_time": "2024-01-01",
  "end_time": "2024-01-31"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `codes` | string[] | 非空数组（必填） |
| `start_time` | string | 可选。支持 `YYYY-MM-DD`、`YYYY-MM-DDTHH:MM:SS`、`YYYYMMDD` 等 ISO 格式；时间部分截断只取日期。缺省时由 SDK 使用默认起始日 `20240101` |
| `end_time` | string | 可选。格式同上。缺省时由 SDK 使用默认结束日 `20991231`；仅当两者都提供时校验 `start_time <= end_time`（按日期比较） |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "kline_time": "2024-01-02", "open": 10.2, "high": 10.45, "low": 10.1, "close": 10.3, "volume": 1234567, "amount": 12700000.0}]}
```

响应 `data` 数组元素字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 证券代码+市场，如 `000001.SZ` |
| `kline_time` | string | 行情日期，`yyyy-MM-dd` 格式（日K只到日期，不含时分秒） |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | int | 成交总量 |
| `amount` | float | 成交总金额 |

**空结果**（HTTP 200）：`{"data": []}`

## POST /minute

查询分钟K数据。

**请求体**：
```json
{
  "codes": ["000001.SZ", "600000.SH"],
  "period": "min5",
  "start_time": "2024-01-02",
  "end_time": "2024-01-02"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `codes` | string[] | 非空数组（必填） |
| `period` | string | 可选。白名单 `min1/min3/min5/min10/min15/min30/min60/min120`，默认 `min1`。非法值返回 422 |
| `start_time` | string | 可选。支持 `YYYY-MM-DD`、`YYYY-MM-DDTHH:MM:SS`、`YYYYMMDD` 等 ISO 格式；时间部分截断只取日期。**`start_time` 与 `end_time` 均缺省时，`begin_date` 默认设为近一年（当前日期前 365 天），避免返回海量分钟数据**；仅传 `start_time` 时 `end_date` 用 SDK 默认（取到最新） |
| `end_time` | string | 可选。格式同上。缺省时由 SDK 使用默认结束日 `20991231`；仅当两者都提供时校验 `start_time <= end_time`（按日期比较） |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "kline_time": "2024-01-02T09:30:00", "kline_time_utc": "2024-01-02T01:30:00", "open": 10.2, "high": 10.45, "low": 10.1, "close": 10.3, "volume": 123456, "amount": 1270000.0}]}
```

响应 `data` 数组元素字段（基于 `/daily` 字段集，额外附加 `kline_time_utc`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 证券代码+市场 |
| `kline_time` | string | ISO datetime 行情时间（交易所本地时间，UTC+8）。分钟K含时分，如 `2024-01-02T09:30:00` |
| `kline_time_utc` | string | UTC 时间，`yyyy-MM-ddTHH:mm:ss` 格式，如 `2024-01-02T01:30:00`。由 `kline_time` 视为 UTC+8 转换而来，供跨时区客户端使用。`kline_time` 为 null 时此字段也为 null |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | int | 成交总量 |
| `amount` | float | 成交总金额 |

**空结果**（HTTP 200）：`{"data": []}`

## POST /adj_factor

查询除权因子（单次复权因子，对应 SDK 手册 3.5.2.6 `BaseData.get_adj_factor`）。每次除权除息事件一行。

**请求体**：
```json
{
  "codes": ["000001.SZ", "600000.SH"],
  "start_time": "2024-01-01",
  "end_time": "2024-06-30"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `codes` | string[] | 非空数组（必填） |
| `start_time` | string | 可选。ISO 日期/日期时间格式。SDK `get_adj_factor` 不支持日期参数，服务端拉取全量后按 `trade_date` 过滤；缺省时该侧不过滤 |
| `end_time` | string | 可选。格式同上。仅当两者都提供时校验 `start_time <= end_time`（按日期比较） |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "trade_date": "2024-05-30", "adj_factor": 1.05}]}
```

响应 `data` 数组元素字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 证券代码+市场，如 `000001.SZ` |
| `trade_date` | string | 除权日期，`yyyy-MM-dd` 格式 |
| `adj_factor` | float | 单次复权因子（每次除权除息事件的比例，消费方自行累乘算累计因子） |

**空结果**（HTTP 200）：`{"data": []}`

> SDK `get_adj_factor` 返回宽表（index=交易日期, columns=股票代码），服务端 `melt` 成长表并 `dropna` 过滤非除权日。字段命名中性（`code`/`trade_date`/`adj_factor`），外部项目通过自身 YAML `field_map` 适配为内部字段（如 stocker 的 `symbol`/`trade_date`/`ex_factor`）。

## GET /realtime

返回实时行情快照。**优先读订阅缓存**（盘中 SDK 实时推送，每个 code 保留最新一笔），**缓存空时 fallback 查当日历史快照**（`query_snapshot` 取收盘快照，覆盖非交易时段）。

> 订阅推送只在交易时段生效；非交易时段缓存为空，自动 fallback 到 `query_snapshot` 查当日快照（取每只股票最后一行）。fallback 结果带 60 秒 TTL 缓存。

**查询参数**：

| 参数 | 类型 | 约束 |
|------|------|------|
| `codes` | string | 可选。逗号分隔的代码列表，如 `?codes=000001.SZ,600000.SH`。不传返回全市场快照；传入则过滤返回指定代码。不触发额外订阅 |

**示例**：
```
GET /realtime                           # 全市场快照
GET /realtime?codes=000001.SZ         # 单个代码
GET /realtime?codes=000001.SZ,600000.SH   # 多个代码
```

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "trade_time": "2024-01-02T09:30:00", "last": 10.3, "pre_close": 10.2, "open": 10.2, "high": 10.45, "low": 10.1, "close": 10.3, "volume": 123456, "amount": 1270000.0, "num_trades": 1234, "high_limited": 11.22, "low_limited": 9.18, "ask_price1": 10.31, "ask_volume1": 500, "bid_price1": 10.29, "bid_volume1": 480, "trading_phase_code": "T0 "}]}
```

响应 `data` 数组元素字段（透传 SDK `Snapshot` 全部字段，字段名保持 SDK 原始名）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 证券代码+市场 |
| `trade_time` | string | ISO datetime，交易所行情数据时间 |
| `pre_close` | float | 昨收价 |
| `last` | float | 最新价 |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | float | 成交总量 |
| `amount` | float | 成交总金额 |
| `num_trades` | float | 成交笔数 |
| `high_limited` | float | 涨停价 |
| `low_limited` | float | 跌停价 |
| `ask_price1`~`ask_price5` | float | 卖1~卖5档价格 |
| `ask_volume1`~`ask_volume5` | int | 卖1~卖5档量 |
| `bid_price1`~`bid_price5` | float | 买1~买5档价格 |
| `bid_volume1`~`bid_volume5` | int | 买1~买5档量 |
| `iopv` | float | 净值估产（仅基金品种有效，其余为 null） |
| `trading_phase_code` | string | 交易阶段代码（见 SDK 文档 §4.1.5） |

> `NaN`/缺失值序列化为 `null`。SDK `Snapshot` 不含 `name`（证券简称）、`change_pct`、`change_amount`、`amplitude`、`turnover_rate` 等衍生字段，由主项目 pipeline 回算。

**数据来源**：
- 交易时段：订阅缓存（SDK 实时推送的最新快照）
- 非交易时段/订阅未推送：fallback `query_snapshot` 查当日历史快照（取每只股票最后一行 = 收盘快照），结果带 60 秒 TTL 缓存
- SDK 未就绪（未登录）：HTTP 503 `SDK_NOT_READY`

## GET /health

健康检查。

**正常**（HTTP 200）：
```json
{"status": "ok", "sdk": "ready", "config": "complete", "realtime": "active"}
```

**异常**（HTTP 503）：
```json
{"status": "degraded", "sdk": "not_ready", "config": "incomplete", "realtime": "inactive"}
```

响应字段：

| 字段 | 取值 | 说明 |
|------|------|------|
| `status` | `ok` / `degraded` | 配置完整且 SDK 就绪时 `ok`，否则 `degraded`。决定 HTTP 200/503 |
| `sdk` | `ready` / `not_ready` | SDK 是否已登录且 MarketData 就绪 |
| `config` | `complete` / `incomplete` | 四项凭据（用户名/密码/IP/端口）是否齐全 |
| `realtime` | `active` / `inactive` | 实时订阅是否运行中。不影响 `status` 和 HTTP 状态码 |

## 认证

`/daily`、`/minute`、`/adj_factor`、`/realtime` 接口需要 Bearer Token 认证。客户端必须在请求头中携带：

```
Authorization: Bearer <AUTH_TOKEN>
```

`/health` 接口免认证（Docker healthcheck 约束）。

### 配置

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `AUTH_TOKEN` | `""` | Bearer token。强度要求：长度 > 12 且同时含字母和数字 |
| `AUTH_REQUIRED` | `true` | 认证开关。`false` 时认证彻底关闭，所有请求直接放行，`AUTH_TOKEN` 被忽略 |

启动时校验 token 强度，弱 token 阻止进程启动。

### 401 响应

缺失或无效 token 时返回 401：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
X-Request-ID: <uuid>
Content-Type: application/json

{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "missing or malformed Authorization header",
    "request_id": "<uuid>"
  }
}
```

`message` 可能值：
- `missing or malformed Authorization header` — 缺失或非 Bearer scheme
- `empty bearer token` — `Bearer ` 后为空
- `invalid bearer token` — token 比对失败

token 比对使用 `hmac.compare_digest`（常数时间，防时序攻击）。scheme 名大小写不敏感（`bearer`/`Bearer`/`BEARER` 均接受，RFC 6750 §2.1）。

## 错误响应格式

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
| `UNAUTHORIZED` | 401 | 缺失或无效的 Bearer token |
| `SDK_NOT_READY` | 503 | SDK 未登录或未初始化 |
| `SDK_QUERY_FAILED` | 502 | 上游 SDK 查询失败 |
| `SERIALIZATION_FAILED` | 502 | 返回值无法安全序列化 |
| `INTERNAL_ERROR` | 500 | 未分类的内部错误 |
| `SERVICE_BUSY` | 503 | 并发 SDK 调用超限（`SDK_MAX_CONCURRENT`），快速失败 |
| `REALTIME_SUBSCRIPTION_FAILED` | 503 | （保留）实时订阅降级标记；`/realtime` 当前改用 fallback 查询 + `SDK_NOT_READY`，不再返回此码 |

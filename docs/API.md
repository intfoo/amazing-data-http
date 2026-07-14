# API 参考

> 本文件从 README 抽离，只描述 HTTP 接口契约。部署、启动、主项目集成等内容见 [README](../README.md)。

## POST /daily

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

## POST /minute

查询分钟K数据。

**请求体**：
```json
{
  "symbols": ["000001.SZ", "600000.SH"],
  "period": "min5",
  "start_time": "2024-01-02",
  "end_time": "2024-01-02"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `symbols` | string[] | 非空数组（必填） |
| `period` | string | 可选。白名单 `min1/min3/min5/min10/min15/min30/min60/min120`，默认 `min1`。非法值返回 422 |
| `start_time` | string | 可选。支持 `YYYY-MM-DD`、`YYYY-MM-DDTHH:MM:SS`、`YYYYMMDD` 等 ISO 格式；时间部分截断只取日期。缺省时由 SDK 使用默认起始日 `20240101` |
| `end_time` | string | 可选。格式同上。缺省时由 SDK 使用默认结束日 `20991231`；仅当两者都提供时校验 `start_time <= end_time`（按日期比较） |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "kline_time": "...", "open": ..., "high": ..., "low": ..., "close": ..., "volume": ..., "amount": ...}]}
```

字段与 `/daily` 完全一致（SDK `query_kline` 对所有周期返回相同列：`code/kline_time/open/high/low/close/volume/amount`）。

**空结果**（HTTP 200）：`{"data": []}`

## GET /health

健康检查。

**正常**（HTTP 200）：
```json
{"status": "ok", "sdk": "ready", "config": "complete"}
```

**异常**（HTTP 503）：
```json
{"status": "degraded", "sdk": "not_ready", "config": "incomplete", "realtime": "inactive"}
```

`realtime` 字段反映实时订阅状态：`active`（订阅运行中）或 `inactive`（未启动/已崩溃）。该字段不影响 `status` 和 HTTP 状态码（realtime 不阻断主健康）。

## GET /realtime

返回全市场实时快照。忽略 `symbols` 查询参数，始终返回全市场缓存快照。

> 主项目 `custom-data-source.md` 约定 GET 请求会发送 `symbols=000001.SZ,600000.SH` query 参数，但本接口始终返回全市场缓存快照，不支持逐个 symbol 拉取。FastAPI 路由不声明该参数即自动忽略。

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "...", "trade_time": "...", "last": ..., "pre_close": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ..., "amount": ..., "num_trades": ..., "high_limited": ..., "low_limited": ..., "ask_price1"~"ask_price5": ..., "ask_volume1"~"ask_volume5": ..., "bid_price1"~"bid_price5": ..., "bid_volume1"~"bid_volume5": ..., "iopv": ..., "trading_phase_code": "..."}]}
```

透传 SDK `Snapshot` 全部字段，字段名保持 SDK 原始名。

**缓存状态**：
- 订阅运行中且已收到数据：返回最新快照列表
- 订阅刚启动未收到数据：返回 `200 {"data": []}`（空缓存）
- 订阅未启动/已崩溃：HTTP 503

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
| `SDK_NOT_READY` | 503 | SDK 未登录或未初始化 |
| `SDK_QUERY_FAILED` | 502 | 上游 SDK 查询失败 |
| `SERIALIZATION_FAILED` | 502 | 返回值无法安全序列化 |
| `INTERNAL_ERROR` | 500 | 未分类的内部错误 |
| `REALTIME_SUBSCRIPTION_FAILED` | 503 | 实时订阅未启动或已崩溃；`/daily`、`/minute` 不受影响 |

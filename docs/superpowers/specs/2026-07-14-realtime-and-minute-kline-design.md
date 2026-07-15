# 实时行情 + 分钟K 接口设计

> 日期：2026-07-14
> 状态：设计稿（已通过子 agent review 并修正）
> 范围：在现有 AmazingData HTTP 适配服务上新增 `POST /minute`（分钟K）和 `GET /realtime`（实时行情）两个端点

## 1. 背景与目标

现有服务仅暴露 `POST /daily`（日K）和 `GET /health`。主项目 `custom-data-source.md` 定义了自定义数据源可接入三类数据集：daily / adj_factor / realtime，并预留 minute 数据集。本次新增：

- **分钟K** `POST /minute` — 历史分钟K查询，复用 SDK `MarketData.query_kline`（period=min1~min120）
- **实时行情** `GET /realtime` — 全市场快照，基于 SDK `SubscribeData` 订阅 + 内存缓存

### 设计决策（已与用户确认）

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 实时行情实现 | 后台订阅线程 + 极简缓存 | 比历史快照冒充实时性更强；比完整订阅状态机简单 |
| 实时字段策略 | 纯透传 SDK Snapshot 原始字段 | 与 /daily 哲学一致（不重命名/不换算/不复权）；衍生值由主项目 pipeline 回算 |
| 分钟K period | 可选，默认 min1，白名单 8 档 | 主项目不传 period 默认 min1 兼容；适配服务支持多 period 供扩展 |
| 分钟K时分参数 | 不暴露 begin_time/end_time 时分戳 | YAGNI；主项目契约只有 symbols/start_time/end_time；按日期过滤即可 |

## 2. API 契约

### 2.1 POST /minute

查询分钟K数据。

**请求体**：
```json
{
  "symbols": ["000001.SZ", "600000.SH"],
  "period": "min5",
  "start_time": "2024-01-02T09:25:00",
  "end_time": "2024-01-02T15:05:00"
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `symbols` | string[] | 非空数组（必填） |
| `period` | string | 可选。白名单 `min1/min3/min5/min10/min15/min30/min60/min120`，默认 `min1`。非法值返回 422 |
| `start_time` | string | 可选。ISO datetime（主项目发 `.isoformat()`），service 层截断为日期 `YYYYMMDD`。缺省时 SDK 默认 `20240101` |
| `end_time` | string | 可选。同上。缺省时 SDK 默认 `20991231`；两者都提供时校验 `start <= end`（按日期比较） |

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "000001.SZ", "kline_time": "...", "open": ..., "high": ..., "low": ..., "close": ..., "volume": ..., "amount": ...}]}
```

字段与 `/daily` 完全一致（SDK `query_kline` 对所有周期返回相同列：`code/kline_time/open/high/low/close/volume/amount`）。

> **field_map 提示**：SDK 返回 `kline_time`（datetime 类型，含时分），主项目 minute 数据集期望内部字段 `datetime`。主项目 YAML 需映射 `kline_time: datetime`（与 /daily 映射 `kline_time: date` 同理，仅目标字段名不同）。

空结果 HTTP 200 `{"data": []}`。错误复用现有错误码表；`period` 非法 → `INVALID_REQUEST` 422；日期反转/格式错 → `INVALID_REQUEST` 422。

### 2.2 GET /realtime

返回全市场实时快照。

> **参数说明**：主项目 `custom-data-source.md` 约定 GET 请求会发送 `symbols=000001.SZ,600000.SH` query 参数，但 realtime 必须是全市场快照接口（不支持逐个 symbol 拉取）。**本接口忽略 `symbols` 参数**，始终返回全市场缓存快照。FastAPI 路由不声明该参数即自动忽略。

**成功响应**（HTTP 200）：
```json
{"data": [{"code": "...", "trade_time": "...", "last": ..., "pre_close": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ..., "amount": ..., "num_trades": ..., "high_limited": ..., "low_limited": ..., "ask_price1"~"ask_price5": ..., "ask_volume1"~"ask_volume5": ..., "bid_price1"~"bid_price5": ..., "bid_volume1"~"bid_volume5": ..., "iopv": ..., "trading_phase_code": "..."}]}
```

透传 SDK `Snapshot` 全部字段（文档 §4.2.1）。字段名保持 SDK 原始名，由主项目 YAML `field_map` 映射。

> **name 字段缺失**：SDK Snapshot（§4.2.1）不含 `name`（证券简称）字段，而主项目 realtime field_map 包含 `name: name`。该映射会缺失，主项目 pipeline 可回算或容忍缺失（`custom-data-source.md` 说"缺失时部分字段由 pipeline 回算"）。如未来需要，可通过 `get_code_info`（§3.5.2.1）预加载 code→name 映射在 service 层补充。当前选择不补充（YAGNI）。

**缓存状态**：
- 订阅运行中且已收到数据：返回最新快照列表（非交易时段返回的是最后一笔快照，可能略过时）
- 订阅刚启动未收到数据：返回 `200 {"data": []}`（空缓存）
- 订阅未启动/已崩溃：HTTP 503 `{"error": {"code": "REALTIME_SUBSCRIPTION_FAILED", "message": "...", "request_id": "..."}}`

### 2.3 新增错误码

| 错误码 | HTTP | 含义 |
|--------|------|------|
| `REALTIME_SUBSCRIPTION_FAILED` | 503 | 实时订阅未启动或已崩溃；`/daily`、`/minute` 不受影响 |

## 3. 架构

```
主项目
  │ POST /daily   POST /minute (symbols, period?, start, end)   GET /realtime
  ▼                     ▼                                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│  http_app.py  路由 + 错误处理                                          │
│   ├── /daily  → KlineService.query(period="day")                      │
│   ├── /minute → KlineService.query(period=req.period or "min1")       │
│   └── /realtime → RealtimeService.snapshot()  ── 读缓存 dict          │
│                         ▲                                              │
│  realtime_service.py    │ daemon 线程 SubscribeData.run() 回调写入     │
│   ├── _cache: dict[code, dict] + Lock                                 │
│   ├── on_snapshot(data) → Snapshot→dict → 覆盖缓存                    │
│   ├── on_subscription_error() → set_active(False)                     │
│   └── snapshot() → list[dict] (浅拷贝)                                │
│  gateway.py                                                           │
│   ├── query_kline (现有, /daily + /minute 共用)                        │
│   ├── get_code_list(security_type) (新, 公共方法)                      │
│   ├── start_snapshot_subscription(code_list, on_data, on_error) (新)  │
│   ├── stop_subscription() (新)                                        │
│   └── _base_data (login 时保存, _safe_logout 时清理)                   │
│  serializer.py (现有 serialize_value, Snapshot→dict 复用)             │
│  health.py (status 增加 realtime 字段)                                 │
└──────────────────────────────────────────────────────────────────────┘
```

## 4. 组件设计

### 4.1 KlineService 扩展（分钟K）

`kline_service.py` 的 `query` 方法新增 `period` 形参，默认 `"day"`，向后兼容：

```python
def query(self, symbols, start_time=None, end_time=None, period="day") -> list[dict]:
    begin_date = to_sdk_date(start_time) if start_time else None
    end_date = to_sdk_date(end_time) if end_time else None
    if begin_date is not None and end_date is not None and begin_date > end_date:
        raise ValueError("start_time must not be later than end_time")
    result = self._gw.query_kline(symbols, begin_date, end_date, period)
    return self._flatten(result)
```

- `/daily` 调用 `query(symbols, start, end)` 不传 period → 默认 `"day"`，**现有行为零改动，现有测试零改动**
- `/minute` 调用 `query(symbols, start, end, period=validated_period)`
- `to_sdk_date` / `_flatten` / `serialize_dataframe` 全部复用

### 4.2 http_app.py 新增

```python
MINUTE_PERIODS = {"min1","min3","min5","min10","min15","min30","min60","min120"}

class MinuteRequest(BaseModel):
    symbols: list[str]
    period: str | None = None    # None → min1
    start_time: str | None = None
    end_time: str | None = None

    @field_validator("symbols")
    @classmethod
    def symbols_nonempty(cls, v):
        if not v or len(v) == 0:
            raise ValueError("symbols must be a non-empty array")
        return v
```

`/minute` 路由：
```python
@app.post("/minute")
async def minute(req: MinuteRequest, request: Request):
    period = req.period or "min1"
    if period not in MINUTE_PERIODS:
        raise AppError(INVALID_REQUEST, f"unsupported period: {period}", 422)
    # 其余 try/except 链与 /daily 完全一致
    data = kline_service.query(req.symbols, req.start_time, req.end_time, period=period)
    return {"data": data}
```

period 白名单校验在 http 层（422）。gateway 的 `PERIOD_MAP`（已包含全部 min1~min120）作为兜底，但 http 层先拦截返回更友好的 422。两层白名单需保持同步（修改时一并更新）。

`http_app.py` 的 import 需新增 `REALTIME_SUBSCRIPTION_FAILED`（见 §4.5）。

### 4.3 RealtimeService（新文件 app/realtime_service.py）

```python
import logging
import threading
from app.serializer import serialize_value

logger = logging.getLogger("amazingdata.realtime")


class RealtimeService:
    def __init__(self, gateway):
        self._gw = gateway
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._active = False

    def on_snapshot(self, data) -> None:
        """订阅回调：Snapshot → dict → 缓存覆盖。异常吞掉，不影响订阅线程。"""
        try:
            record = self._snapshot_to_dict(data)
            code = record.get("code") if record else None
            if code:
                with self._lock:
                    self._cache[code] = record
        except Exception as e:
            logger.warning("on_snapshot convert failed: %s: %s", type(e).__name__, e)

    def on_subscription_error(self, err=None) -> None:
        """订阅线程崩溃/异常退出回调：标记不活跃，/realtime 将返回 503。"""
        self._active = False
        logger.error("realtime subscription deactivated due to error: %s", err)

    def snapshot(self) -> list[dict]:
        """GET /realtime 读缓存，返回全市场快照列表。

        对每个缓存 dict 做浅拷贝（dict(v)），避免外部序列化修改污染缓存。
        """
        with self._lock:
            return [dict(v) for v in self._cache.values()]

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active

    @staticmethod
    def _snapshot_to_dict(data) -> dict:
        """Snapshot 对象 → JSON 安全 dict。

        SDK 回调传入 ad.constant.Snapshot 对象，字段访问方式未在 probe 中验证。
        采用三级降级策略提取字段：
        1. dataclasses.is_dataclass(data) → dataclasses.asdict(data)
        2. 有 __dict__ → vars(data)
        3. 有 __slots__ → 遍历 type(data).__mro__ 收集所有 __slots__（含继承）

        提取后对每个值调用 serialize_value 转换：
        - datetime/Timestamp → isoformat 字符串
        - NaN/NaT → None
        - numpy 标量 → python 原生
        返回 {} 表示无法提取（调用方 on_snapshot 跳过 code 为空的记录）。
        """
        import dataclasses
        if dataclasses.is_dataclass(data):
            raw = dataclasses.asdict(data)
        elif hasattr(data, "__dict__"):
            raw = dict(vars(data))
        else:
            # 遍历 MRO 收集所有 __slots__（含父类）
            slots = []
            for cls in type(data).__mro__:
                slots.extend(getattr(cls, "__slots__", []))
            raw = {s: getattr(data, s) for s in slots if hasattr(data, s)} if slots else {}
        return {k: serialize_value(v) for k, v in raw.items()}
```

**缓存语义**：`dict[code, dict]` 覆盖写入，每个 code 只保留最新快照，无需淘汰策略。

### 4.4 Gateway 扩展

`Gateway` Protocol 新增三个方法：

```python
class Gateway(Protocol):
    def login(self) -> None: ...
    def logout(self) -> None: ...
    def is_ready(self) -> bool: ...
    def query_kline(self, symbols, begin_date, end_date, period) -> dict[str, pd.DataFrame]: ...
    def get_code_list(self, security_type: str = "EXTRA_STOCK_A") -> list[str]: ...
    def start_snapshot_subscription(self, code_list: list[str], on_data, on_error=None) -> None: ...
    def stop_subscription(self) -> None: ...
```

`AmazingDataGateway` 实现：
- `__init__` 新增 `self._base_data = None`、`self._subscribe_data = None`、`self._sub_thread = None`
- `_do_login` 中保存 `self._base_data = ad.BaseData()`（现有代码已创建 base 但作为局部变量未保存，改为保存为实例属性）
- `_safe_logout` 末尾追加 `self._base_data = None`（与 `self._market_data = None` 一同清理，避免 logout 后悬空引用）
- `get_code_list`：检查 `if not self._ready or self._base_data is None: raise GatewayNotReadyError(...)`，否则委托 `self._base_data.get_code_list(security_type)`
- `start_snapshot_subscription(code_list, on_data, on_error=None)`：
  ```python
  def start_snapshot_subscription(self, code_list, on_data, on_error=None) -> None:
      if not self._ready or self._ad is None:
          raise GatewayNotReadyError("gateway not ready for subscription")
      # 与现有 query_kline 统一用 from AmazingData.utils.constant import Period
      from AmazingData.utils.constant import Period
      sub = self._ad.SubscribeData()
      @sub.register(code_list=code_list, period=Period.snapshot.value)
      def _on_snapshot(data, period):
          try:
              on_data(data)
          except Exception as e:
              logger.warning("snapshot callback error: %s: %s", type(e).__name__, e)
      self._subscribe_data = sub
      def _run():
          try:
              sub.run()
          except Exception as e:
              logger.error("subscription thread crashed: %s: %s", type(e).__name__, e)
              if on_error:
                  try:
                      on_error(e)
                  except Exception:
                      pass
      self._sub_thread = threading.Thread(target=_run, daemon=True, name="snapshot-sub")
      self._sub_thread.start()
  ```
  - `on_error` 回调在订阅线程异常退出时触发，用于通知 `RealtimeService.set_active(False)`
  - Period import 统一用 `from AmazingData.utils.constant import Period`（与现有 `query_kline` 实现一致，不依赖 `ad.constant` 路径）
- `stop_subscription`：尝试 SDK `stop()`（若有），清理 `self._subscribe_data`/`self._sub_thread` 引用

**FakeGateway 扩展**（`tests/conftest.py`）：
- `get_code_list(security_type="EXTRA_STOCK_A")`：返回可注入列表，默认 `["000001.SZ", "600000.SH"]`（构造时可通过参数覆盖）
- `start_snapshot_subscription(code_list, on_data, on_error=None)`：no-op + `self.sub_start_called += 1` + 记录 code_list
- `stop_subscription()`：no-op + `self.sub_stop_called += 1`

### 4.5 http_app.py 订阅生命周期

import 需新增 `REALTIME_SUBSCRIPTION_FAILED`：
```python
from app.errors import (
    AppError, INTERNAL_ERROR, INVALID_REQUEST, REALTIME_SUBSCRIPTION_FAILED,
    RequestIdMiddleware, SDK_NOT_READY, SDK_QUERY_FAILED, SERIALIZATION_FAILED, get_request_id,
)
```

`create_app` 中创建顺序：先建 `realtime_service`，再传给 `HealthService(config, gateway, realtime_service)`：
```python
realtime_service = RealtimeService(gateway)
health_service = HealthService(config, gateway, realtime_service)
app.state.realtime_service = realtime_service
```

startup：
```python
@startup
async def startup_login():
    ...
    if config.is_configured():
        try:
            gateway.login()
            try:
                code_list = gateway.get_code_list(security_type="EXTRA_STOCK_A")
                gateway.start_snapshot_subscription(
                    code_list,
                    on_data=realtime_service.on_snapshot,
                    on_error=realtime_service.on_subscription_error,
                )
                realtime_service.set_active(True)
                logger.info("realtime subscription started: %d symbols", len(code_list))
            except Exception as e:
                logger.error("realtime subscription start failed: %s: %s", type(e).__name__, e)
        except Exception as e:
            logger.error("gateway login failed: %s: %s", type(e).__name__, e)
```

shutdown：先 `stop_subscription()` 再 `logout()`。

`/realtime` 路由：
```python
@app.get("/realtime")
async def realtime(request: Request):
    if not realtime_service.is_active():
        raise AppError(REALTIME_SUBSCRIPTION_FAILED, "realtime subscription not active", 503)
    data = realtime_service.snapshot()
    return {"data": data}
```

### 4.6 健康检查扩展

`health.py` 的 `HealthService.__init__` 增加可选参数 `realtime_service=None`（默认 None，向后兼容现有 `HealthService(config, gateway)` 调用）。`status()` 增加 `"realtime"` 字段；`is_ok()` 不变（realtime 不阻断主健康）：

```python
def __init__(self, config, gateway, realtime_service=None):
    self._config = config
    self._gw = gateway
    self._realtime_svc = realtime_service

def status(self) -> dict:
    ready = self._config.is_configured() and self._gw.is_ready()
    rt = self._realtime_svc.is_active() if self._realtime_svc else False
    return {
        "status": "ok" if ready else "degraded",
        "sdk": "ready" if self._gw.is_ready() else "not_ready",
        "config": "complete" if self._config.is_configured() else "incomplete",
        "realtime": "active" if rt else "inactive",
    }
```

## 5. 数据流

### 5.1 分钟K

```
POST /minute → MinuteRequest 校验 → period 白名单校验(422)
  → KlineService.query(symbols, start, end, period)
  → to_sdk_date(start/end) → begin_date/end_date (int YYYYMMDD)
  → Gateway.query_kline(symbols, begin_date, end_date, period)
  → PERIOD_MAP[period] → Period.minN.value
  → SDK market_data.query_kline(symbols, period=..., begin_date=..., end_date=...)
  → dict[code, DataFrame]
  → _flatten → serialize_dataframe → list[dict]
  → {"data": [...]}
```

### 5.2 实时行情

```
[启动] gateway.login() → get_code_list(EXTRA_STOCK_A)
  → start_snapshot_subscription(code_list, on_snapshot, on_error)
  → daemon 线程 SubscribeData.run()
  → SDK 推送 Snapshot → 回调 _on_snapshot(data)
  → RealtimeService.on_snapshot(data) → _snapshot_to_dict → serialize_value
  → _cache[code] = record (覆盖)

[崩溃] sub.run() 抛异常 → _run except → on_error(e)
  → RealtimeService.on_subscription_error() → set_active(False)

[请求] GET /realtime (忽略 symbols 参数)
  → is_active()? 否 → 503 REALTIME_SUBSCRIPTION_FAILED
  → 是 → snapshot() → [dict(v) for v in _cache.values()] → {"data": [...]}
```

## 6. 错误处理

| 场景 | HTTP | 错误码 |
|------|------|--------|
| /minute period 非法 | 422 | INVALID_REQUEST |
| /minute 日期反转/格式错 | 422 | INVALID_REQUEST |
| /minute SDK 未就绪 | 503 | SDK_NOT_READY |
| /minute SDK 查询失败 | 502 | SDK_QUERY_FAILED |
| /realtime 订阅未启动/崩溃 | 503 | REALTIME_SUBSCRIPTION_FAILED |
| 订阅回调异常 | 不影响响应 | 记 warning，吞掉 |
| 订阅线程崩溃 | 不影响 /daily | on_error → is_active=False → /realtime 返回 503 |

## 7. 测试策略

### 7.1 分钟K（tests/test_kline_service.py + test_http_app.py）

- `test_query_minute_passes_period` — period 透传 gateway.query_calls
- `test_query_minute_default_period` — 不传 period 默认 "min1"
- `test_query_minute_keeps_day_default` — /daily 路径 period 仍为 "day"（回归）
- `test_minute_invalid_period_422` — HTTP /minute 非法 period
- `test_minute_success` — HTTP /minute 返回数据
- `test_minute_optional_period` — 不传 period 字段默认 min1
- `test_minute_reversed_dates` — 日期反转 422（复用 /daily 模式）

### 7.2 实时行情（tests/test_realtime_service.py + test_http_app.py）

- `test_on_snapshot_stores_in_cache` — 注入 Snapshot-like 对象，snapshot() 能读出
- `test_snapshot_empty_cache` — 空缓存返回 []
- `test_snapshot_overwrites` — 同 code 多次回调只保留最新
- `test_snapshot_returns_shallow_copy` — 返回的 dict 修改不污染缓存
- `test_on_subscription_error_deactivates` — on_subscription_error 后 is_active=False
- `test_realtime_not_active_503` — is_active=False 时 /realtime 返回 503
- `test_realtime_active_returns_data` — is_active=True 时返回缓存数据
- `test_realtime_ignores_symbols_param` — GET /realtime?symbols=xxx 仍返回全市场
- `test_snapshot_to_dict_handles_datetime_nan` — datetime→isoformat, NaN→None
- `test_snapshot_to_dict_dataclass` / `_dict` / `_slots` — 三级降级
- FakeGateway 的 start/stop_subscription 记录调用 + get_code_list 返回注入列表

### 7.3 现有测试

- /daily 全部测试零改动（period 默认 "day"）
- FakeGateway 新增方法为 no-op/默认返回，现有 /daily 测试不受影响

## 8. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `app/kline_service.py` | 修改 | `query` 加 `period="day"` 形参（1 行） |
| `app/realtime_service.py` | 新增 | RealtimeService 类 |
| `app/gateway.py` | 修改 | Protocol 加 get_code_list/start_snapshot_subscription/stop_subscription；AmazingDataGateway 实现；_base_data 保存+清理；_safe_logout 加清理 |
| `app/http_app.py` | 修改 | 新增 /minute + /realtime 路由、MinuteRequest、订阅生命周期、import REALTIME_SUBSCRIPTION_FAILED |
| `app/health.py` | 修改 | __init__ 加 realtime_service=None；status 增加 realtime 字段 |
| `app/errors.py` | 修改 | 新增 REALTIME_SUBSCRIPTION_FAILED 错误码常量 |
| `tests/conftest.py` | 修改 | FakeGateway 加 get_code_list/start/stop_subscription + make_snapshot helper |
| `tests/test_kline_service.py` | 修改 | 加 minute 相关测试 |
| `tests/test_http_app.py` | 修改 | 加 /minute + /realtime 测试 |
| `tests/test_realtime_service.py` | 新增 | RealtimeService 单元测试 |
| `docs/API.md` | 修改 | 新增 /minute + /realtime 文档 |

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `SubscribeData.run()` 阻塞行为未知 | 订阅可能无法在 daemon 线程跑 | 实现时容错；启动失败降级 503；不影响 /daily。**实现前先用 probe 脚本验证** `SubscribeData.run()` 在线程中的阻塞行为和回调频率 |
| Snapshot 对象字段访问方式未知 | `_snapshot_to_dict` 可能失败 | 三级降级（dataclasses.asdict → vars → MRO slots）；回调异常吞掉记 warning |
| 订阅全市场开销/频率未知 | 回调过载 | 回调极轻（dict 赋值）；若过载可限订阅成分股子集（后续配置项） |
| SDK 线程安全（订阅+查询并存） | 可能冲突 | 订阅用 SubscribeData，查询用 MarketData，独立对象；现有锁只保护 MarketData |
| tgw 连接数限制 | 双对象可能占连接 | 单 login 复用会话；shutdown 释放 |
| SDK 文档标注 begin_date/end_date 为必填 | 若 SDK 升级强制要求会破坏 None 透传 | probe 验证当前 SDK 有默认值（20240101/20991231），此行为依赖 SDK 实现而非文档契约；若 SDK 升级后强制要求，需改为 always 传日期 |
| `Period.snapshot` 访问路径 | `from AmazingData.utils.constant import Period` 是否含 snapshot 成员未在 probe 验证 | probe-report 验证了 min1~year，未验证 snapshot/snapshotfuture/snapshotHKT；实现时 probe 确认，若缺失改用 `ad.constant.Period` 路径 |
| 订阅断线/`run()` 正常退出但无数据 | `_active` 仍 True，返回过时缓存 | 不做自动重连（YAGNI），依赖进程重启；on_error 仅覆盖异常退出，正常退出场景靠 /health 的 realtime 字段人工发现 |

## 10. 不做的事（YAGNI）

- 不暴露分钟K的 begin_time/end_time 时分戳参数（主项目契约不需要）
- 不支持动态订阅/退订（HTTP symbols 参数被忽略，返回全市场缓存）
- 不补算 change_pct/amplitude/turnover_rate（主项目 pipeline 回算）
- 不补 name 字段（SDK Snapshot 不含，需额外 get_code_info 调用，YAGNI）
- 不做缓存淘汰/TTL（快照覆盖语义，天然最新）
- 不做 realtime 的 WebSocket/SSE 推送（主项目是 pull 轮询模型，rpm:60）
- 不做订阅自动重连（依赖进程重启）

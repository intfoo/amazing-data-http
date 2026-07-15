# 实时行情 + 分钟K 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AmazingData HTTP 适配服务新增 `POST /minute`（分钟K）和 `GET /realtime`（实时行情）端点。

**Architecture:** 分钟K复用 KlineService（加 period 参数）；实时行情用后台 daemon 线程跑 SDK SubscribeData 订阅，回调写入 RealtimeService 内存缓存，GET /realtime 读缓存返回全市场快照。

**Tech Stack:** Python 3.13/3.14, FastAPI, Pydantic, pandas, AmazingData SDK, pytest。

**Spec:** `docs/superpowers/specs/2026-07-14-realtime-and-minute-kline-design.md`（含完整代码片段，执行时参考）。

## Global Constraints

- 适配服务保留 SDK 原始字段名，不重命名/不换算/不复权（与现有 /daily 哲学一致）
- 错误响应统一格式 `{"error": {"code","message","request_id"}}`
- 分钟K period 白名单：`min1/min3/min5/min10/min15/min30/min60/min120`，默认 min1
- 实时行情透传 SDK Snapshot 全部原始字段，不补算衍生值
- /daily 现有行为与测试零改动（period 默认 "day" 向后兼容）
- 测试用 FakeGateway，不依赖真实 SDK

## 任务依赖与并行策略

```
Task 1 (kline_service)  ┐
Task 2 (realtime_service)┼─ 并行（文件不冲突）
Task 3 (errors+gateway)  ┘
        │
        ▼
Task 4 (http_app+health+docs)  串行（依赖 1,2,3，改 http_app/health 集成层）
```

---

### Task 1: KlineService 加 period 参数（分钟K service 层）

**Files:**
- Modify: `app/kline_service.py`
- Test: `tests/test_kline_service.py`

**Interfaces:**
- Produces: `KlineService.query(symbols, start_time=None, end_time=None, period="day") -> list[dict]`（新增 period 形参，默认 "day"）

- [ ] **Step 1: 写失败测试**（追加到 test_kline_service.py 末尾）

```python
def test_query_minute_passes_period():
    """分钟K应把 period 透传给 gateway。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-01", "2024-01-31", period="min5")
    call = gw.query_calls[0]
    assert call["period"] == "min5"


def test_query_minute_default_period_is_day():
    """不传 period 时默认 'day'，保持 /daily 向后兼容。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")
    assert gw.query_calls[0]["period"] == "day"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node node_modules/jest/bin/jest.js` ❌ 这是 Python 项目，用：
Run: `python -m pytest tests/test_kline_service.py::test_query_minute_passes_period -v`
Expected: FAIL（TypeError: query() got unexpected keyword 'period'）

- [ ] **Step 3: 实现**（kline_service.py 第 44-64 行 query 方法加 period 形参）

将 `def query(self, symbols, start_time=None, end_time=None) -> list[dict]:` 改为 `def query(self, symbols, start_time=None, end_time=None, period="day") -> list[dict]:`，方法体内 `result = self._gw.query_kline(symbols, begin_date, end_date, "day")` 改为 `result = self._gw.query_kline(symbols, begin_date, end_date, period)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_kline_service.py -v`
Expected: 全部 PASS（含原有测试 + 2 个新测试）

- [ ] **Step 5: 提交**

`git add app/kline_service.py tests/test_kline_service.py && git commit -m "feat: KlineService.query 支持 period 参数（分钟K）"`

---

### Task 2: RealtimeService 新建（实时行情缓存层，独立新文件）

**Files:**
- Create: `app/realtime_service.py`
- Test: `tests/test_realtime_service.py`

**Interfaces:**
- Consumes: `app.serializer.serialize_value`
- Produces: `RealtimeService(gateway)` 含 `on_snapshot(data)` / `on_subscription_error(err=None)` / `snapshot() -> list[dict]` / `is_active() -> bool` / `set_active(bool)`

- [ ] **Step 1: 写失败测试**（tests/test_realtime_service.py）

```python
from dataclasses import dataclass
from datetime import datetime
import math

from app.realtime_service import RealtimeService


@dataclass
class FakeSnapshot:
    code: str
    trade_time: datetime
    last: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int
    amount: float


def test_on_snapshot_stores_in_cache():
    svc = RealtimeService(gateway=None)
    snap = FakeSnapshot(code="000001.SZ", trade_time=datetime(2024,1,2,9,30),
                        last=10.3, pre_close=10.2, open=10.2, high=10.5,
                        low=10.1, volume=1000, amount=10300.0)
    svc.on_snapshot(snap)
    data = svc.snapshot()
    assert len(data) == 1
    assert data[0]["code"] == "000001.SZ"
    assert data[0]["last"] == 10.3


def test_snapshot_empty_cache():
    svc = RealtimeService(gateway=None)
    assert svc.snapshot() == []


def test_snapshot_overwrites_same_code():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(FakeSnapshot("000001.SZ", datetime(2024,1,2,9,30), 10.0, 9.9, 9.9, 10.1, 9.8, 100, 1000.0))
    svc.on_snapshot(FakeSnapshot("000001.SZ", datetime(2024,1,2,9,31), 10.5, 10.0, 10.0, 10.6, 9.9, 200, 2100.0))
    data = svc.snapshot()
    assert len(data) == 1
    assert data[0]["last"] == 10.5  # 最新覆盖


def test_snapshot_returns_shallow_copy():
    """返回的 dict 修改不污染缓存。"""
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(FakeSnapshot("000001.SZ", datetime(2024,1,2,9,30), 10.0, 9.9, 9.9, 10.1, 9.8, 100, 1000.0))
    data = svc.snapshot()
    data[0]["last"] = 999.0
    assert svc.snapshot()[0]["last"] == 10.0  # 缓存未被污染


def test_on_subscription_error_deactivates():
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    assert svc.is_active() is True
    svc.on_subscription_error()
    assert svc.is_active() is False


def test_snapshot_to_dict_handles_nan():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(FakeSnapshot("000001.SZ", datetime(2024,1,2,9,30), float("nan"), 9.9, 9.9, 10.1, 9.8, 100, 1000.0))
    data = svc.snapshot()
    assert data[0]["last"] is None  # NaN → None


def test_snapshot_to_dict_datetime_isoformat():
    svc = RealtimeService(gateway=None)
    svc.on_snapshot(FakeSnapshot("000001.SZ", datetime(2024,1,2,9,30,0), 10.0, 9.9, 9.9, 10.1, 9.8, 100, 1000.0))
    data = svc.snapshot()
    assert data[0]["trade_time"] == "2024-01-02T09:30:00"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_realtime_service.py -v`
Expected: FAIL（ModuleNotFoundError: app.realtime_service）

- [ ] **Step 3: 实现**（app/realtime_service.py，完整代码见 spec §4.3）

创建 `app/realtime_service.py`，含 `RealtimeService` 类：
- `__init__(self, gateway)`：`_cache: dict[str,dict]={}`、`_lock=threading.Lock()`、`_active=False`
- `on_snapshot(data)`：try `_snapshot_to_dict` → 取 code → `with _lock: _cache[code]=record`；except 记 warning
- `on_subscription_error(err=None)`：`_active=False` + 记 error
- `snapshot()`：`with _lock: return [dict(v) for v in _cache.values()]`（浅拷贝）
- `is_active()` / `set_active(bool)`
- `_snapshot_to_dict(data)` 静态方法：三级降级（dataclasses.asdict → vars → MRO __slots__），每值过 `serialize_value`

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_realtime_service.py -v`
Expected: 全部 PASS（7 个测试）

- [ ] **Step 5: 提交**

`git add app/realtime_service.py tests/test_realtime_service.py && git commit -m "feat: 新增 RealtimeService（实时行情订阅缓存层）"`

---

### Task 3: Gateway 扩展 + FakeGateway + 错误码（SDK 订阅封装）

**Files:**
- Modify: `app/errors.py`
- Modify: `app/gateway.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `Gateway` Protocol 新增 `get_code_list(security_type="EXTRA_STOCK_A") -> list[str]`、`start_snapshot_subscription(code_list, on_data, on_error=None) -> None`、`stop_subscription() -> None`
- Produces: `REALTIME_SUBSCRIPTION_FAILED` 错误码常量
- FakeGateway 实现上述三方法（no-op + 记录调用）

- [ ] **Step 1: 写失败测试**（追加到 tests/conftest.py 同目录的 test_gateway_interface.py 或新测试）

```python
# tests/test_gateway_interface.py 追加
def test_fake_gateway_get_code_list():
    from tests.conftest import FakeGateway
    gw = FakeGateway(ready=True)
    codes = gw.get_code_list()
    assert isinstance(codes, list)
    assert len(codes) > 0


def test_fake_gateway_subscription_noop():
    from tests.conftest import FakeGateway
    gw = FakeGateway(ready=True)
    gw.start_snapshot_subscription(["000001.SZ"], on_data=lambda d: None)
    assert gw.sub_start_called == 1
    gw.stop_subscription()
    assert gw.sub_stop_called == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_gateway_interface.py -v`
Expected: FAIL（FakeGateway 无 get_code_list / sub_start_called）

- [ ] **Step 3: 实现 errors.py**（新增一行常量，在 SERIALIZATION_FAILED 之后）

```python
REALTIME_SUBSCRIPTION_FAILED = "REALTIME_SUBSCRIPTION_FAILED"  # 503：实时订阅未启动或已崩溃
```

- [ ] **Step 4: 实现 gateway.py 扩展**

按 spec §4.4：
1. `Gateway` Protocol 加三方法签名：`get_code_list`、`start_snapshot_subscription`、`stop_subscription`
2. `AmazingDataGateway.__init__` 加 `self._base_data = None`、`self._subscribe_data = None`、`self._sub_thread = None`
3. `_do_login` 中 `base = ad.BaseData()` 后保存 `self._base_data = base`
4. `_safe_logout` 末尾加 `self._base_data = None`
5. 新增 `get_code_list`：检查 `if not self._ready or self._base_data is None: raise GatewayNotReadyError(...)`，委托 `self._base_data.get_code_list(security_type)`
6. 新增 `start_snapshot_subscription(code_list, on_data, on_error=None)`：用 `from AmazingData.utils.constant import Period` 取 `Period.snapshot.value`，`@sub.register` 绑定回调，daemon 线程跑 `sub.run()`，except 调 `on_error(e)`（完整代码见 spec §4.4）
7. 新增 `stop_subscription`：尝试 SDK stop()（若有），清理引用

- [ ] **Step 5: 实现 conftest.py FakeGateway 扩展**

FakeGateway `__init__` 加 `self._code_list = code_list or ["000001.SZ", "600000.SH"]`、`self.sub_start_called = 0`、`self.sub_stop_called = 0`。新增方法：
```python
def get_code_list(self, security_type="EXTRA_STOCK_A"):
    return list(self._code_list)

def start_snapshot_subscription(self, code_list, on_data, on_error=None):
    self.sub_start_called += 1
    self._sub_code_list = code_list

def stop_subscription(self):
    self.sub_stop_called += 1
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_gateway_interface.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

`git add app/errors.py app/gateway.py tests/conftest.py tests/test_gateway_interface.py && git commit -m "feat: Gateway 扩展订阅方法 + REALTIME_SUBSCRIPTION_FAILED 错误码"`

---

### Task 4: HTTP 路由 + 生命周期 + 健康检查 + 文档（集成层）

**Files:**
- Modify: `app/http_app.py`
- Modify: `app/health.py`
- Modify: `tests/test_http_app.py`
- Modify: `docs/API.md`

**Interfaces:**
- Consumes: Task 1 的 `KlineService.query(period=)`、Task 2 的 `RealtimeService`、Task 3 的 `Gateway.get_code_list/start_snapshot_subscription/stop_subscription` + `REALTIME_SUBSCRIPTION_FAILED`
- Produces: `POST /minute`、`GET /realtime` 路由；`HealthService(config, gateway, realtime_service=None)`

- [ ] **Step 1: 写失败测试**（追加到 tests/test_http_app.py）

```python
def test_minute_success():
    client = make_test_app()
    resp = client.post("/minute", json={
        "symbols": ["000001.SZ"], "period": "min5",
        "start_time": "2024-01-02", "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


def test_minute_default_period_min1():
    client = make_test_app()
    resp = client.post("/minute", json={"symbols": ["000001.SZ"], "start_time": "2024-01-02", "end_time": "2024-01-02"})
    assert resp.status_code == 200


def test_minute_invalid_period():
    client = make_test_app()
    resp = client.post("/minute", json={"symbols": ["000001.SZ"], "period": "day"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_minute_empty_symbols():
    client = make_test_app()
    resp = client.post("/minute", json={"symbols": []})
    assert resp.status_code == 422


def test_realtime_not_active_503():
    """订阅未激活时 /realtime 返回 503。"""
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    # make_test_app 不触发真实 startup 订阅（TestClient 触发 startup，但 FakeGateway no-op 不设 active）
    resp = client.get("/realtime")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "REALTIME_SUBSCRIPTION_FAILED"


def test_realtime_active_returns_data():
    """订阅激活 + 注入缓存数据后 /realtime 返回数据。"""
    gw = FakeGateway(ready=True)
    app = create_app(config=Config(username="u",password="p",ip="1.2.3.4",port=3021), gateway=gw)
    # 手动注入缓存并激活
    from datetime import datetime
    app.state.realtime_service.on_snapshot(type("S",(),{
        "code":"000001.SZ","trade_time":datetime(2024,1,2,9,30),
        "last":10.3,"pre_close":10.2,"open":10.2,"high":10.5,"low":10.1,
        "volume":1000,"amount":10300.0,
    }.items()) if False else _make_snap())  # 用 dataclass 更干净
    app.state.realtime_service.set_active(True)
    client = TestClient(app)
    resp = client.get("/realtime")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


def _make_snap():
    from dataclasses import dataclass
    from datetime import datetime
    @dataclass
    class Snap:
        code: str; trade_time: datetime; last: float; pre_close: float
        open: float; high: float; low: float; volume: int; amount: float
    return Snap("000001.SZ", datetime(2024,1,2,9,30), 10.3, 10.2, 10.2, 10.5, 10.1, 1000, 10300.0)


def test_realtime_ignores_symbols_param():
    """GET /realtime?symbols=xxx 仍返回全市场（忽略参数）。"""
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/realtime?symbols=000001.SZ")
    assert resp.status_code in (200, 503)  # 503 因未激活，但不报 422


def test_health_has_realtime_field():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    assert "realtime" in resp.json()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_http_app.py -v`
Expected: FAIL（无 /minute 路由 404、无 /realtime 路由 404、health 无 realtime 字段）

- [ ] **Step 3: 实现 http_app.py**（按 spec §4.2 + §4.5）

1. import 加 `REALTIME_SUBSCRIPTION_FAILED` 和 `RealtimeService`
2. 新增 `MINUTE_PERIODS = {"min1","min3","min5","min10","min15","min30","min60","min120"}`
3. 新增 `MinuteRequest(BaseModel)`（symbols/period=None/start_time=None/end_time=None + symbols_nonempty validator）
4. `create_app` 中：建 `realtime_service = RealtimeService(gateway)`，`HealthService(config, gateway, realtime_service)`，存 `app.state.realtime_service`
5. startup_login 中 login 成功后：`code_list = gateway.get_code_list()` → `gateway.start_snapshot_subscription(code_list, on_data=realtime_service.on_snapshot, on_error=realtime_service.on_subscription_error)` → `realtime_service.set_active(True)`；try/except 包裹记 error（不阻断启动）
6. shutdown_logout 中先 `gateway.stop_subscription()` 再 `gateway.logout()`
7. 新增 `@app.post("/minute")` 路由：period 校验 → `kline_service.query(symbols, start, end, period=period)` → `{"data": data}`（try/except 链同 /daily）
8. 新增 `@app.get("/realtime")` 路由：`if not realtime_service.is_active(): raise AppError(REALTIME_SUBSCRIPTION_FAILED, ..., 503)` → `realtime_service.snapshot()` → `{"data": data}`

- [ ] **Step 4: 实现 health.py**（按 spec §4.6）

`__init__(self, config, gateway, realtime_service=None)` 加可选参数存 `self._realtime_svc`。`status()` 加 `"realtime": "active" if (self._realtime_svc and self._realtime_svc.is_active()) else "inactive"`。

- [ ] **Step 5: 更新 docs/API.md**

新增 `## POST /minute` 和 `## GET /realtime` 章节（字段表、请求/响应示例、错误码），错误码表加 `REALTIME_SUBSCRIPTION_FAILED`。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_http_app.py tests/test_kline_service.py tests/test_realtime_service.py tests/test_gateway_interface.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 跑完整测试套件回归**

Run: `python -m pytest -v`
Expected: 全部 PASS（原有 82 个 + 新增测试，无回归失败）

- [ ] **Step 8: 提交**

`git add app/http_app.py app/health.py tests/test_http_app.py docs/API.md && git commit -m "feat: 新增 /minute + /realtime 路由 + 订阅生命周期 + 健康检查扩展"`

---

## Self-Review

**1. Spec 覆盖**：§2 API 契约 → Task 4；§4.1 KlineService → Task 1；§4.2 http_app MinuteRequest → Task 4；§4.3 RealtimeService → Task 2；§4.4 Gateway → Task 3；§4.5 生命周期 → Task 4；§4.6 health → Task 4；§7 测试 → 各 Task。全覆盖。

**2. Placeholder 扫描**：test_realtime_active_returns_data 的 Snap 构造用了 `_make_snap()` helper（完整定义）。无 TBD/TODO。

**3. 类型一致性**：`start_snapshot_subscription(code_list, on_data, on_error=None)` 在 Task 3 定义、Task 4 调用，签名一致。`on_subscription_error(err=None)` Task 2 定义、Task 4 传作 on_error，一致。`get_code_list(security_type="EXTRA_STOCK_A")` 一致。

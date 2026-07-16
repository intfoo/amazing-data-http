# 性能与设计优化 P0-P2 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除序列化性能瓶颈、迁移废弃 API、补齐 SDK 自愈/并发保护/时区健壮性等设计缺陷，使分钟K响应从秒级降到百毫秒级、服务在网络抖动下自愈、高并发下快速失败不雪崩。

**Architecture:** 在现有 FastAPI + Gateway 分层上做就地优化，不改变接口契约和字段语义。序列化改 pandas 向量化；启动事件迁移到 lifespan；SDK 查询失败时惰性重连；realtime 缓存读写降低锁竞争；路由层加并发闸门快速失败。

**Tech Stack:** Python 3.13+ / FastAPI 0.115+ / pandas 2.2+ / numpy 2.0+ / pytest 8.0+

## Global Constraints

- Python `>=3.13`，fastapi `>=0.115`，pandas `>=2.2`，numpy `>=2.0`（见 `pyproject.toml`）
- 保留 SDK 原始字段名，不重命名、不换算单位、不复权（README 职责边界）
- 凭据只通过环境变量注入，不写入源码或日志
- 错误响应统一格式 `{"error": {"code","message","request_id"}}`
- 工作区绝对路径：`d:\_yz\stocker\amazingDataHttp`
- 测试风格：`test_xxx` 函数（非类），`from app.xxx import yyy`，`from tests.conftest import FakeGateway, make_daily_df`
- Config 测试构造：`Config(username="u", password="p", ip="1.2.3.4", port=3021)` 直接实例化
- 现有测试基线：107 个（本计划新增后会增加），全绿才能合并

---

## File Structure

| 文件 | 改动类型 | 职责 |
|------|---------|------|
| `app/serializer.py` | 修改 | `serialize_dataframe` 改向量化（Task 1） |
| `app/kline_service.py` | 修改 | `_flatten` 浅拷贝（Task 2）、minute 默认区间显式时区（Task 3） |
| `app/http_app.py` | 修改 | 迁移 lifespan（Task 4）、SdkGate 并发保护（Task 10） |
| `app/errors.py` | 修改 | 新增 `SERVICE_BUSY` 错误码（Task 10） |
| `app/gateway.py` | 修改 | 惰性重连 `_is_connection_error` + query 重试（Task 6） |
| `app/realtime_service.py` | 修改 | 浅拷贝出锁（Task 7）、fallback concat（Task 8）、类型缓存（Task 9） |
| `Dockerfile` | 修改 | CMD 读环境变量（Task 5） |
| `docker-compose.yml` | 修改 | 端口映射读环境变量 + 删 version（Task 5） |
| `tests/test_serializer.py` | 修改 | 新增向量化边界测试（Task 1） |
| `tests/test_kline_service.py` | 修改 | 新增浅拷贝/时区测试（Task 2/3） |
| `tests/test_http_app.py` | 修改 | 新增 lifespan shutdown / 并发保护测试（Task 4/10） |
| `tests/test_realtime_service.py` | 修改 | 新增出锁/concat/类型缓存测试（Task 7/8/9） |
| `tests/test_amazingdata_gateway.py` | 修改 | 新增重连测试（Task 6） |

---

## Task 1: 序列化向量化（P0）

**Files:**
- Modify: `app/serializer.py:56-66`
- Test: `tests/test_serializer.py`

**Interfaces:**
- Consumes: 无新依赖
- Produces: `serialize_dataframe(df) -> list[dict]` 签名不变，行为不变，内部改向量化

**背景：** 当前 `serialize_dataframe` 对每条记录每个字段调 `serialize_value`，分钟K近一年 100 只股票约 4600 万次 Python 函数调用。向量化后数值列走 pandas C 层，仅 object 列兜底。

- [ ] **Step 1: 补保护网测试（object 列含 Timestamp 兜底 + 大数据量正确性）**

在 `tests/test_serializer.py` 末尾追加：

```python
def test_serialize_dataframe_object_column_with_timestamp():
    """object dtype 列中残留 Timestamp 应被 serialize_value 兜底为 ISO 字符串。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "mixed": [pd.Timestamp("2024-01-02T09:30:00")],
    })
    result = serialize_dataframe(df)
    assert result == [{"code": "000001.SZ", "mixed": "2024-01-02T09:30:00"}]


def test_serialize_dataframe_large_volume_correctness():
    """大数据量向量化后行为与逐条一致：numpy 标量转原生、NaN 转 None、datetime 转 ISO。"""
    n = 5000
    df = pd.DataFrame({
        "code": ["000001.SZ"] * n,
        "kline_time": pd.date_range("2024-01-02", periods=n, freq="D"),
        "open": [10.2] * n,
        "close": [float("nan")] * n,
        "volume": np.int64(1234567) * np.ones(n, dtype=np.int64),
    })
    result = serialize_dataframe(df)
    assert len(result) == n
    assert isinstance(result[0]["volume"], int)
    assert result[0]["volume"] == 1234567
    assert result[0]["close"] is None
    assert result[0]["kline_time"] == "2024-01-02T00:00:00"
    assert result[-1]["kline_time"].startswith("20")
```

- [ ] **Step 2: 运行测试确认当前实现通过（建立保护网）**

Run: `python -m pytest tests/test_serializer.py -v`
Expected: 全部 PASS（含新增 2 个）

- [ ] **Step 3: 重构 `serialize_dataframe` 为向量化实现**

替换 `app/serializer.py` 的 `serialize_dataframe` 函数（当前 56-66 行）为：

```python
def serialize_dataframe(df: pd.DataFrame) -> list[dict]:
    """将 DataFrame 转为 JSON 安全的 list[dict]（向量化序列化）。

    若 DataFrame 有命名索引（如 trade_time），先 reset_index 将索引变为普通列。
    向量化策略（替代逐字段 serialize_value，大幅减少 Python 函数调用）：
    - datetime64 列 → dt.strftime ISO 字符串（NaT → NaN，后续 where 填 None）
    - 全表 astype(object)：numpy 标量 → Python 原生（int64→int, float64→float）
    - 原生 object dtype 列可能残留 datetime/date/np 标量 → map(serialize_value) 兜底
    - NaN/NaT → None（where 向量化填充）
    """
    if df is None or df.empty:
        return []
    df_to_use = df.reset_index() if df.index.name is not None else df
    df_to_use = df_to_use.copy()
    fallback_cols: list[str] = []
    for col in list(df_to_use.columns):
        s = df_to_use[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            df_to_use[col] = s.dt.strftime("%Y-%m-%dT%H:%M:%S")
        elif s.dtype == object:
            fallback_cols.append(col)
    df_obj = df_to_use.astype(object)
    for col in fallback_cols:
        df_obj[col] = df_obj[col].map(serialize_value)
    df_obj = df_obj.where(pd.notna(df_obj), None)
    return df_obj.to_dict(orient="records")
```

- [ ] **Step 4: 运行全部 serializer 测试确认通过**

Run: `python -m pytest tests/test_serializer.py -v`
Expected: 全部 PASS（含原有 16 个 + 新增 2 个）

- [ ] **Step 5: 运行 kline/realtime/http_app 测试确认无回归**

Run: `python -m pytest tests/test_kline_service.py tests/test_realtime_service.py tests/test_http_app.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add app/serializer.py tests/test_serializer.py
git commit -m "perf: vectorize serialize_dataframe with pandas dtype-aware processing"
```

---

## Task 2: _flatten 浅拷贝优化（P2）

**Files:**
- Modify: `app/kline_service.py:118`
- Test: `tests/test_kline_service.py`

**Interfaces:**
- Consumes: Task 1 的 `serialize_dataframe`
- Produces: `_flatten` 行为不变，`df.copy()` → `df.copy(deep=False)`

**背景：** `_flatten` 每只股票 `df.copy()` 深拷贝整个 DataFrame，仅为原地改 `kline_time` 列。但 `strftime`/`tz_convert` 返回新 Series，不污染原数据块，浅拷贝足够。

- [ ] **Step 1: 补保护网测试（验证浅拷贝不污染 gateway 返回的原始 DataFrame）**

在 `tests/test_kline_service.py` 末尾追加：

```python
def test_flatten_does_not_mutate_gateway_result():
    """_flatten 改 kline_time 列后，gateway 返回的原始 DataFrame 不应被污染。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.Timestamp("2024-01-02")],
        "open": [10.2], "high": [10.4], "low": [10.1],
        "close": [10.3], "volume": [100], "amount": [1000.0],
    })
    original_time = df["kline_time"].iloc[0]
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    # 原始 df 的 kline_time 仍是 Timestamp，未被 strftime 改成字符串
    assert df["kline_time"].iloc[0] == original_time
    assert isinstance(df["kline_time"].iloc[0], pd.Timestamp)
```

- [ ] **Step 2: 运行测试确认当前实现通过**

Run: `python -m pytest tests/test_kline_service.py::test_flatten_does_not_mutate_gateway_result -v`
Expected: PASS

- [ ] **Step 3: 改 `df.copy()` 为 `df.copy(deep=False)`**

在 `app/kline_service.py` 的 `_flatten` 方法中（约 118 行），将：

```python
            df = df.copy()
```

改为：

```python
            df = df.copy(deep=False)
```

- [ ] **Step 4: 运行 kline 测试确认通过**

Run: `python -m pytest tests/test_kline_service.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/kline_service.py tests/test_kline_service.py
git commit -m "perf: use shallow copy in _flatten to avoid deep DataFrame duplication"
```

---

## Task 3: minute 默认区间显式时区（P1）

**Files:**
- Modify: `app/kline_service.py:82`
- Test: `tests/test_kline_service.py`

**Interfaces:**
- Consumes: 已定义的 `_SHANGHAI_TZ`（`kline_service.py:32`）
- Produces: 无新接口

**背景：** `datetime.now()` 是 naive，依赖容器时区（Dockerfile 设了 Asia/Shanghai）。若时区配置丢失（如本地裸跑未设 TZ），默认近一年的 `begin_date` 算错。文件已定义 `_SHANGHAI_TZ`，应显式使用。

- [ ] **Step 1: 补测试（验证 begin_date 基于 UTC+8 计算，与运行环境 TZ 无关）**

在 `tests/test_kline_service.py` 末尾追加：

```python
def test_query_minute_default_range_uses_shanghai_timezone(monkeypatch):
    """minute 默认 begin_date 应基于 UTC+8 计算，不受运行环境 TZ 影响。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    # 模拟 UTC 时间 2024-07-16 16:00:00（= 北京时间 2024-07-17 00:00:00）
    from app.kline_service import _SHANGHAI_TZ
    from datetime import datetime, timezone, timedelta
    fake_utc_now = datetime(2024, 7, 16, 16, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.kline_service.datetime", lambda *a, **kw: fake_utc_now if not a else datetime(*a, **kw))
    svc.query(["000001.SZ"], period="min1")
    call = gw.query_calls[0]
    # 北京时间 2024-07-17 前 365 天 ≈ 2023-07-18
    expected = int((fake_utc_now.astimezone(_SHANGHAI_TZ) - timedelta(days=365)).strftime("%Y%m%d"))
    assert abs(call["begin_date"] - expected) <= 1
```

- [ ] **Step 2: 运行测试确认失败（当前用 naive now，不受 TZ 控制）**

Run: `python -m pytest tests/test_kline_service.py::test_query_minute_default_range_uses_shanghai_timezone -v`
Expected: FAIL（monkeypatch datetime 后行为不符合预期，或因 now() 不接受 tz）

- [ ] **Step 3: 改 `datetime.now()` 为 `datetime.now(_SHANGHAI_TZ)`**

在 `app/kline_service.py` 的 `query` 方法中（约 82 行），将：

```python
            now = datetime.now()
```

改为：

```python
            now = datetime.now(_SHANGHAI_TZ)
```

同时确认文件顶部已 import（当前已有 `from datetime import datetime, timedelta, timezone`，无需改）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_kline_service.py::test_query_minute_default_range_uses_shanghai_timezone tests/test_kline_service.py::test_query_minute_default_range_last_year -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/kline_service.py tests/test_kline_service.py
git commit -m "fix: use explicit Shanghai timezone for minute default date range"
```

---

## Task 4: 迁移 lifespan（P0）

**Files:**
- Modify: `app/http_app.py:110-200`
- Test: `tests/test_http_app.py`

**Interfaces:**
- Consumes: 无
- Produces: `create_app` 签名不变，内部用 `lifespan` 替代 `on_event`

**背景：** `@app.on_event("startup"/"shutdown")` 在 FastAPI 0.93+ 已 deprecated，`pyproject.toml` 要求 `fastapi>=0.115`，运行时打 deprecation warning。迁移到 `lifespan` async context manager。

- [ ] **Step 1: 补 shutdown 保护网测试（验证 lifespan 触发 logout）**

在 `tests/test_http_app.py` 末尾追加：

```python
def test_shutdown_calls_gateway_logout():
    """lifespan shutdown 应调用 gateway.logout() 释放 SDK 连接。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        # startup 触发 login
        assert gw.login_called >= 1
    # 退出 with 块后 shutdown 触发 logout
    assert gw.logout_called >= 1
```

- [ ] **Step 2: 运行测试确认当前实现通过（on_event 也支持 with 语法）**

Run: `python -m pytest tests/test_http_app.py::test_shutdown_calls_gateway_logout -v`
Expected: PASS

- [ ] **Step 3: 迁移到 lifespan**

在 `app/http_app.py` 的 `create_app` 函数中，做以下改动：

a. 文件顶部 import 区追加（约 11 行 `import asyncio` 附近）：

```python
from contextlib import asynccontextmanager
```

b. 将 `create_app` 内的 `@app.on_event("startup")` + `@app.on_event("shutdown")` 两段（约 134-200 行）替换为 lifespan 定义，并调整 app 创建顺序。当前 `create_app` 开头是：

```python
    app = FastAPI(title="AmazingData HTTP Adapter")
    app.add_middleware(RequestIdMiddleware)

    if config is None:
        config = Config.from_env()
    if gateway is None:
        gateway = AmazingDataGateway(config)

    kline_service = KlineService(gateway)
    realtime_service = RealtimeService(gateway)
    health_service = HealthService(config, gateway, realtime_service)

    app.state.config = config
    app.state.gateway = gateway
    app.state.kline_service = kline_service
    app.state.realtime_service = realtime_service
    app.state.health_service = health_service

    @app.on_event("startup")
    async def startup_login():
        ...
    @app.on_event("shutdown")
    async def shutdown_logout():
        ...
```

替换为（注意：config/gateway 的 None 处理移到最前，lifespan 闭包捕获 service 变量，app 创建时传入 lifespan）：

```python
    if config is None:
        config = Config.from_env()
    if gateway is None:
        gateway = AmazingDataGateway(config)

    kline_service = KlineService(gateway)
    realtime_service = RealtimeService(gateway)
    health_service = HealthService(config, gateway, realtime_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时登录 SDK + 后台订阅初始化；退出时停止订阅 + 登出。

        替代已废弃的 on_event("startup"/"shutdown")。uvicorn 触发 lifespan
        startup/shutdown，TestClient 的 with 语法同样触发。
        """
        _align_uvicorn_log_format()
        if config.is_configured():
            try:
                gateway.login()
                logger.info("gateway login succeeded on startup")
                def _init_subscription():
                    t0 = time.monotonic()
                    try:
                        code_list = gateway.get_realtime_code_list()
                        t1 = time.monotonic()
                        gateway.start_snapshot_subscription(
                            code_list,
                            on_data=realtime_service.on_snapshot,
                            on_error=realtime_service.on_subscription_error,
                        )
                        realtime_service.set_active(True)
                        t2 = time.monotonic()
                        logger.info(
                            "realtime subscription started: %d symbols "
                            "(get_realtime_code_list=%.3fs subscribe=%.3fs)",
                            len(code_list), t1 - t0, t2 - t1,
                        )
                    except Exception as e:
                        logger.error("realtime subscription start failed: %s: %s", type(e).__name__, e)
                app.state.subscription_thread = threading.Thread(
                    target=_init_subscription, daemon=True, name="sub-init"
                )
                app.state.subscription_thread.start()
            except Exception as e:
                logger.error("gateway login failed on startup: %s: %s", type(e).__name__, e)
        else:
            logger.warning("config incomplete, skipping startup login")
        yield
        # shutdown
        sub_thread = getattr(app.state, "subscription_thread", None)
        if sub_thread and sub_thread.is_alive():
            sub_thread.join(timeout=10)
        try:
            gateway.stop_subscription()
        except Exception as e:
            logger.warning("stop subscription on shutdown: %s: %s", type(e).__name__, e)
        try:
            gateway.logout()
            logger.info("gateway logout on shutdown")
        except Exception as e:
            logger.warning("gateway logout failed on shutdown: %s: %s", type(e).__name__, e)

    app = FastAPI(title="AmazingData HTTP Adapter", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)

    app.state.config = config
    app.state.gateway = gateway
    app.state.kline_service = kline_service
    app.state.realtime_service = realtime_service
    app.state.health_service = health_service
```

路由定义部分（`@app.get("/health")` 等）保持不变，在 `app.state` 设置之后。

- [ ] **Step 4: 运行全部 http_app 测试确认通过**

Run: `python -m pytest tests/test_http_app.py -v`
Expected: 全部 PASS（含原有 + 新增 shutdown 测试）

- [ ] **Step 5: Commit**

```bash
git add app/http_app.py tests/test_http_app.py
git commit -m "refactor: migrate from deprecated on_event to lifespan context manager"
```

---

## Task 5: Dockerfile/compose 读环境变量（P1）

**Files:**
- Modify: `Dockerfile:52`
- Modify: `docker-compose.yml:1,17`
- Test: 手动验证 `docker compose config`

**Interfaces:**
- Consumes: 无
- Produces: Docker 模式下 `HTTP_HOST`/`HTTP_PORT` 生效

**背景：** Dockerfile CMD 写死 `0.0.0.0:3021`，`.env` 的 `HTTP_PORT` 在 Docker 模式无效。docker-compose `version` 字段已废弃。

- [ ] **Step 1: 修改 Dockerfile CMD 读环境变量**

在 `Dockerfile` 中（约 52 行），将：

```dockerfile
CMD ["uvicorn", "app.http_app:app", "--host", "0.0.0.0", "--port", "3021"]
```

改为：

```dockerfile
# 读取 HTTP_HOST/HTTP_PORT 环境变量（.env 注入），默认 0.0.0.0:3021
CMD ["sh", "-c", "uvicorn app.http_app:app --host ${HTTP_HOST:-0.0.0.0} --port ${HTTP_PORT:-3021}"]
```

同时删除上方注释中"此处 host/port 写死，未读取 .env"的说明（约 49-51 行），替换为：

```dockerfile
# host/port 从环境变量读取（docker-compose env_file 注入 .env 的 HTTP_HOST/HTTP_PORT）。
EXPOSE 3021
```

- [ ] **Step 2: 修改 docker-compose.yml**

在 `docker-compose.yml` 中：

a. 删除第 1 行 `version: "3.8"`（现代 compose spec 不再需要，会打 warning）。

b. 将 ports 映射（约 17 行）从：

```yaml
    ports:
      - "3021:3021"
```

改为：

```yaml
    ports:
      - "${HTTP_PORT:-3021}:${HTTP_PORT:-3021}"
```

c. 更新 ports 上方注释（约 11-15 行）为：

```yaml
    # 端口映射：宿主机端口:容器内端口，均读取 HTTP_PORT（默认 3021）。
    # Dockerfile CMD 已读取 HTTP_HOST/HTTP_PORT，二者保持一致。
```

- [ ] **Step 3: 验证 compose 配置语法**

Run: `docker compose config`
Expected: 输出有效配置，无 warning，ports 显示 `"3021:3021"`（HTTP_PORT 未设时用默认）

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "fix: make HTTP_HOST/HTTP_PORT effective in Docker mode, drop deprecated compose version"
```

---

## Task 6: SDK 惰性重连（P1）

**Files:**
- Modify: `app/gateway.py:255-300`（query_kline）, `209-253`（query_snapshot）
- Test: `tests/test_amazingdata_gateway.py`

**Interfaces:**
- Consumes: 无
- Produces: 新增模块级 `_is_connection_error(exc) -> bool`；`query_kline`/`query_snapshot` 在连接类错误时 relogin + 重试一次

**背景：** SDK 连接因网络抖动断开后 `is_ready` 仍 True，查询才报错，无自愈。惰性重连：查询失败时若异常像连接类错误，持锁 relogin + 重试一次。

- [ ] **Step 1: 写 `_is_connection_error` 失败测试**

在 `tests/test_amazingdata_gateway.py` 追加：

```python
def test_is_connection_error_detects_keywords():
    """_is_connection_error 应识别常见连接类错误关键词。"""
    from app.gateway import _is_connection_error
    assert _is_connection_error(RuntimeError("Connection reset by peer"))
    assert _is_connection_error(RuntimeError("Operation timed out"))
    assert _is_connection_error(RuntimeError("broken pipe"))
    assert _is_connection_error(RuntimeError("server closed connection"))
    assert not _is_connection_error(ValueError("unsupported period"))
    assert not _is_connection_error(RuntimeError("invalid code"))
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py::test_is_connection_error_detects_keywords -v`
Expected: FAIL（`_is_connection_error` 未定义）

- [ ] **Step 3: 实现 `_is_connection_error`**

在 `app/gateway.py` 的 `PERIOD_MAP` 定义之后（约 39 行）、`Gateway` Protocol 之前，追加：

```python
# 连接类错误关键词（小写匹配）。命中时触发惰性重连：持锁 relogin + 重试一次。
# 基于常见网络异常消息，保守匹配，误判也只是多一次 relogin 尝试。
_CONNECTION_KEYWORDS: tuple[str, ...] = (
    "connection", "timeout", "timed out", "disconnect", "disconnected",
    "broken pipe", "eof", "reset", "unreachable", "refused", "closed",
)


def _is_connection_error(exc: Exception) -> bool:
    """判断异常是否可能是网络/连接类错误（应触发重连）。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in _CONNECTION_KEYWORDS)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py::test_is_connection_error_detects_keywords -v`
Expected: PASS

- [ ] **Step 5: 写 query_kline 重连测试**

在 `tests/test_amazingdata_gateway.py` 追加：

```python
def test_query_kline_reconnects_on_connection_error(monkeypatch):
    """query_kline 遇连接类错误时应 relogin + 重试一次，重试成功则返回结果。"""
    import pandas as pd
    from app.gateway import AmazingDataGateway
    gw = AmazingDataGateway(make_config())
    gw._ready = True
    calls = {"login": 0, "query": 0}

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            calls["query"] += 1
            if calls["query"] == 1:
                raise RuntimeError("Connection reset by peer")
            return {"000001.SZ": pd.DataFrame({"code": ["000001.SZ"], "close": [10.3]})}

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(gw, "_do_login", lambda: calls.__setitem__("login", calls["login"] + 1))
    result = gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")
    assert calls["login"] == 1
    assert calls["query"] == 2
    assert "000001.SZ" in result


def test_query_kline_raises_after_reconnect_failure(monkeypatch):
    """relogin 后重试仍失败时应抛 GatewayQueryError。"""
    from app.gateway import AmazingDataGateway, GatewayQueryError
    gw = AmazingDataGateway(make_config())
    gw._ready = True

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            raise RuntimeError("Connection refused")

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(gw, "_do_login", lambda: None)
    with pytest.raises(GatewayQueryError, match="after reconnect"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")


def test_query_kline_non_connection_error_does_not_relogin(monkeypatch):
    """非连接类错误不应触发 relogin。"""
    from app.gateway import AmazingDataGateway, GatewayQueryError
    gw = AmazingDataGateway(make_config())
    gw._ready = True
    login_called = [0]

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            raise ValueError("invalid code format")

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(gw, "_do_login", lambda: login_called.__setitem__(0, login_called[0] + 1))
    with pytest.raises(GatewayQueryError, match="query failed"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")
    assert login_called[0] == 0
```

- [ ] **Step 6: 运行确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py -v -k "reconnect or non_connection"`
Expected: FAIL（重连逻辑未实现）

- [ ] **Step 7: 实现 query_kline 重连逻辑**

在 `app/gateway.py` 的 `query_kline` 方法中（约 287-300 行），将 `with self._lock:` 块替换为：

```python
        with self._lock:
            try:
                result = self._market_data.query_kline(codes, **kwargs)
                return result if isinstance(result, dict) else {"_all": result}
            except Exception as e:
                logger.error(
                    "query_kline failed: %s: %s (codes=%d, begin=%s, end=%s, period=%s)",
                    type(e).__name__, e, len(codes),
                    begin_date if begin_date is not None else "default",
                    end_date if end_date is not None else "default",
                    period,
                )
                if _is_connection_error(e):
                    logger.warning("query_kline connection error, attempting relogin: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_kline(codes, **kwargs)
                        logger.info("query_kline succeeded after relogin")
                        return result if isinstance(result, dict) else {"_all": result}
                    except Exception as e2:
                        logger.error("query_kline failed after reconnect: %s: %s", type(e2).__name__, e2)
                        raise GatewayQueryError(f"query failed after reconnect: {e2}") from e2
                raise GatewayQueryError(f"query failed: {e}") from e
```

- [ ] **Step 8: 实现 query_snapshot 重连逻辑**

在 `app/gateway.py` 的 `query_snapshot` 方法中（约 236-242 行），将 `with self._lock:` 块替换为：

```python
        with self._lock:
            try:
                result = self._market_data.query_snapshot(codes, **kwargs)
            except Exception as e:
                logger.error("query_snapshot failed: %s: %s (codes=%d, date=%s)",
                             type(e).__name__, e, len(codes), trade_date)
                if _is_connection_error(e):
                    logger.warning("query_snapshot connection error, attempting relogin: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_snapshot(codes, **kwargs)
                        logger.info("query_snapshot succeeded after relogin")
                    except Exception as e2:
                        logger.error("query_snapshot failed after reconnect: %s: %s", type(e2).__name__, e2)
                        raise GatewayQueryError(f"query_snapshot failed after reconnect: {e2}") from e2
                else:
                    raise GatewayQueryError(f"query_snapshot failed: {e}") from e
```

- [ ] **Step 9: 运行 gateway 测试确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py tests/test_gateway_interface.py -v`
Expected: 全部 PASS

- [ ] **Step 10: Commit**

```bash
git add app/gateway.py tests/test_amazingdata_gateway.py
git commit -m "feat: lazy reconnect on connection-like SDK errors with one retry"
```

---

## Task 7: realtime 浅拷贝移出锁（P2）

**Files:**
- Modify: `app/realtime_service.py:62-73`
- Test: `tests/test_realtime_service.py`

**Interfaces:**
- Consumes: 无
- Produces: `snapshot` 行为不变，锁内只取引用，浅拷贝在锁外

**背景：** 全市场几千只股票 `dict(v)` 浅拷贝在 `_lock` 内执行，延长锁持有时间阻塞 `on_snapshot` 写入。锁内只取 values 快照引用，锁外做浅拷贝。

- [ ] **Step 1: 补保护网测试（验证 snapshot 返回浅拷贝，外部修改不污染缓存）**

在 `tests/test_realtime_service.py` 末尾追加（如已有 `test_snapshot_returns_shallow_copy` 则跳过此步，但确认它存在）：

```python
def test_snapshot_large_cache_returns_correct_count():
    """大量 code 缓存时 snapshot 返回数量正确（验证移出锁后不丢数据）。"""
    svc = RealtimeService(gateway=None)
    for i in range(100):
        svc.on_snapshot(_snap(code=f"{i:06d}.SZ"))
    result = svc.snapshot()
    assert len(result) == 100
```

- [ ] **Step 2: 运行确认当前通过**

Run: `python -m pytest tests/test_realtime_service.py::test_snapshot_large_cache_returns_correct_count tests/test_realtime_service.py::test_snapshot_returns_shallow_copy -v`
Expected: PASS

- [ ] **Step 3: 改 `snapshot` 浅拷贝移出锁**

在 `app/realtime_service.py` 的 `snapshot` 方法中（约 62-73 行），替换为：

```python
    def snapshot(self, codes: list[str] | None = None) -> list[dict]:
        """GET /realtime 读缓存，返回快照列表。

        codes 为 None 时返回全市场快照；非空时只返回指定 code 的快照。
        锁内只取 values 引用快照（list 浅复制引用），浅拷贝 dict 在锁外完成，
        避免长时间持锁阻塞 on_snapshot 写入。
        """
        with self._lock:
            if codes is None:
                items = list(self._cache.values())
            else:
                wanted = set(codes)
                items = [v for code, v in self._cache.items() if code in wanted]
        return [dict(v) for v in items]
```

- [ ] **Step 4: 运行 realtime 测试确认通过**

Run: `python -m pytest tests/test_realtime_service.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/realtime_service.py tests/test_realtime_service.py
git commit -m "perf: move dict shallow-copy out of lock in RealtimeService.snapshot"
```

---

## Task 8: fallback concat 优化（P2）

**Files:**
- Modify: `app/realtime_service.py:133-138`
- Test: `tests/test_realtime_service.py`

**Interfaces:**
- Consumes: Task 1 的 `serialize_dataframe`
- Produces: `fallback_snapshot` 行为不变，多只股票合并后一次序列化

**背景：** fallback 逐只 `df.tail(1)` + `serialize_dataframe`，几千只 = 几千次序列化调用。合并为一个大 DataFrame 一次序列化。

- [ ] **Step 1: 补测试（验证多只股票 fallback 返回正确且每只取最后一行）**

在 `tests/test_realtime_service.py` 末尾追加：

```python
def test_fallback_concat_takes_last_row_per_code():
    """fallback 应对每只股票取最后一行（最新快照），多只合并序列化。"""
    import pandas as pd
    gw = FakeGateway(ready=True)
    svc = RealtimeService(gateway=gw)
    gw.query_snapshot = lambda codes, **kw: {
        "000001.SZ": pd.DataFrame({
            "code": ["000001.SZ", "000001.SZ"],
            "last": [10.0, 10.3],
            "trade_time": [pd.Timestamp("2024-01-02T09:30"), pd.Timestamp("2024-01-02T15:00")],
        }),
        "600000.SH": pd.DataFrame({
            "code": ["600000.SH"],
            "last": [20.5],
            "trade_time": [pd.Timestamp("2024-01-02T15:00")],
        }),
    }
    result = svc.fallback_snapshot(["000001.SZ", "600000.SH"])
    assert len(result) == 2
    by_code = {r["code"]: r for r in result}
    assert by_code["000001.SZ"]["last"] == 10.3  # 取最后一行
    assert by_code["600000.SH"]["last"] == 20.5
```

- [ ] **Step 2: 运行确认当前通过**

Run: `python -m pytest tests/test_realtime_service.py::test_fallback_concat_takes_last_row_per_code -v`
Expected: PASS

- [ ] **Step 3: 改 fallback 为 concat 一次序列化**

在 `app/realtime_service.py` 的 `fallback_snapshot` 方法中（约 133-141 行），将：

```python
            records: list[dict] = []
            for code, df in result.items():
                if df is None or df.empty:
                    continue
                # 取最后一行（最新快照），用 serialize_dataframe 序列化
                records.extend(serialize_dataframe(df.tail(1)))
            self._fallback_cache = records
            self._fallback_time = time.time()
            logger.info("fallback query_snapshot: %d records cached", len(records))
```

替换为：

```python
            # 合并每只股票的最后一行（最新快照），一次 serialize_dataframe 序列化，
            # 避免几千只股票逐只调 serialize_dataframe 的开销。
            tails = [df.tail(1) for df in result.values() if df is not None and not df.empty]
            if tails:
                import pandas as pd
                merged = pd.concat(tails, ignore_index=True)
                records = serialize_dataframe(merged)
            else:
                records = []
            self._fallback_cache = records
            self._fallback_time = time.time()
            logger.info("fallback query_snapshot: %d records cached", len(records))
```

- [ ] **Step 4: 运行 realtime 测试确认通过**

Run: `python -m pytest tests/test_realtime_service.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/realtime_service.py tests/test_realtime_service.py
git commit -m "perf: concat tail rows before single serialize in fallback_snapshot"
```

---

## Task 9: _snapshot_to_dict 类型缓存（P2）

**Files:**
- Modify: `app/realtime_service.py:155-180`
- Test: `tests/test_realtime_service.py`

**Interfaces:**
- Consumes: 无
- Produces: `_snapshot_to_dict` 行为不变，按 type 缓存提取函数

**背景：** 每个快照回调都走 `is_dataclass`/`hasattr`/`__mro__` 遍历。全市场高频推送下是热路径。按类型缓存提取函数，首次确定后后续直接调用。

- [ ] **Step 1: 补测试（验证混合类型多次调用不串类型）**

在 `tests/test_realtime_service.py` 末尾追加：

```python
def test_snapshot_to_dict_caches_per_type():
    """同类型多次调用应复用提取函数，混合类型（股票+指数）不串类型。"""
    svc = RealtimeService(gateway=None)
    # 交替推送股票和指数快照，验证类型缓存不混淆
    for i in range(10):
        svc.on_snapshot(_snap(code=f"{i:06d}.SZ"))
        svc.on_snapshot(_index_snap(code=f"{i:06d}.SH"))
    result = svc.snapshot()
    stock = [r for r in result if r["code"].endswith(".SZ")]
    index = [r for r in result if r["code"].endswith(".SH")]
    assert len(stock) == 10
    assert len(index) == 10
    # 指数快照含 trading_phase_code，股票不含
    assert "trading_phase_code" in index[0]
    assert "trading_phase_code" not in stock[0]
```

- [ ] **Step 2: 运行确认当前通过**

Run: `python -m pytest tests/test_realtime_service.py::test_snapshot_to_dict_caches_per_type -v`
Expected: PASS

- [ ] **Step 3: 改 `_snapshot_to_dict` 为按类型缓存**

在 `app/realtime_service.py` 的 `RealtimeService.__init__` 中（约 36-44 行），在 `self._fallback_lock = threading.Lock()` 之后追加：

```python
        # 按类型缓存快照提取函数（dataclass/vars/slots），避免每帧类型探测
        self._extract_fns: dict = {}
```

然后将 `_snapshot_to_dict` 方法（约 155-180 行）替换为：

```python
    def _snapshot_to_dict(self, data) -> dict:
        """Snapshot 对象 → JSON 安全 dict（按类型缓存提取函数）。

        首次遇到某类型时确定提取策略（dataclass.asdict / vars / slots 遍历），
        缓存到 _extract_fns[type]，后续同类型直接调用，避免每帧 is_dataclass/hasattr 开销。
        提取后对每个值调 serialize_value 转换 datetime/NaN/numpy。
        """
        t = type(data)
        fn = self._extract_fns.get(t)
        if fn is None:
            fn = self._build_extract_fn(data)
            self._extract_fns[t] = fn
        raw = fn(data)
        return {k: serialize_value(v) for k, v in raw.items()}

    @staticmethod
    def _build_extract_fn(data):
        """根据 data 类型构建字段提取函数（首次调用，结果可缓存）。"""
        if dataclasses.is_dataclass(data):
            return dataclasses.asdict
        if hasattr(data, "__dict__"):
            return lambda d: dict(vars(d))
        slots = []
        for cls in type(data).__mro__:
            slots.extend(getattr(cls, "__slots__", []))
        if slots:
            return lambda d: {s: getattr(d, s) for s in slots if hasattr(d, s)}
        return lambda d: {}
```

- [ ] **Step 4: 运行 realtime 测试确认通过**

Run: `python -m pytest tests/test_realtime_service.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/realtime_service.py tests/test_realtime_service.py
git commit -m "perf: cache snapshot extraction function per type in RealtimeService"
```

---

## Task 10: 并发保护 SdkGate（P2）

**Files:**
- Modify: `app/errors.py:13-19`（新增错误码）
- Modify: `app/http_app.py`（路由加 SdkGate）
- Test: `tests/test_http_app.py`

**Interfaces:**
- Consumes: Task 4 的 lifespan 结构
- Produces: 新增 `SdkGate` 类、`SERVICE_BUSY` 错误码

**背景：** gateway `_lock` 串行化所有 SDK 调用，高并发时默认线程池 40 线程全排队堆积。加并发闸门：在飞 SDK 调用超过上限时快速返回 503，避免雪崩。

- [ ] **Step 1: 写 SdkGate 失败测试**

在 `tests/test_http_app.py` 顶部 import 区追加（如未有）：

```python
from app.errors import SERVICE_BUSY
```

在文件末尾追加：

```python
def test_sdk_gate_rejects_when_concurrency_exceeded():
    """并发 SDK 调用超过上限时应返回 503 SERVICE_BUSY。"""
    import threading
    import time as _time
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    # 用门控让第一个请求持锁不释放，第二个请求应被拒
    hold = threading.Event()
    release = threading.Event()
    original = gw.query_kline
    def slow_query(codes, begin_date, end_date, period):
        hold.set()
        release.wait(timeout=5)
        return original(codes, begin_date, end_date, period)
    gw.query_kline = slow_query
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    app = create_app(config=config, gateway=gw)
    app.state.sdk_gate = __import__("app.http_app", fromlist=["SdkGate"]).SdkGate(max_concurrent=1)
    client = TestClient(app)
    results = {}
    def worker(idx):
        resp = client.post("/daily", json={"codes": ["000001.SZ"]})
        results[idx] = resp.status_code
    t1 = threading.Thread(target=worker, args=(1,))
    t1.start()
    hold.wait(timeout=5)
    # 第二个请求应被拒（并发上限 1）
    resp2 = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp2.status_code == 503
    assert resp2.json()["error"]["code"] == SERVICE_BUSY
    release.set()
    t1.join(timeout=5)
    assert results.get(1) == 200


def test_sdk_gate_allows_under_limit():
    """并发数在上限内时应正常返回 200。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    app = create_app(config=config, gateway=gw)
    app.state.sdk_gate = __import__("app.http_app", fromlist=["SdkGate"]).SdkGate(max_concurrent=5)
    client = TestClient(app)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_http_app.py::test_sdk_gate_rejects_when_concurrency_exceeded tests/test_http_app.py::test_sdk_gate_allows_under_limit -v`
Expected: FAIL（`SdkGate` / `SERVICE_BUSY` 未定义）

- [ ] **Step 3: 新增 `SERVICE_BUSY` 错误码**

在 `app/errors.py` 的错误码定义区（约 19 行 `INTERNAL_ERROR` 之后）追加：

```python
SERVICE_BUSY = "SERVICE_BUSY"                  # 503：并发 SDK 调用超限，快速失败
```

- [ ] **Step 4: 实现 `SdkGate` 类**

在 `app/http_app.py` 的 `create_app` 函数定义之前（约 110 行，`MinuteRequest` 类之后），追加：

```python
class SdkGate:
    """并发 SDK 调用闸门：在飞调用超过上限时快速失败返回 503，避免线程堆积雪崩。

    gateway._lock 已串行化 SDK 调用，但默认线程池 40 线程会全部排队堆积。
    SdkGate 在路由层限制"在飞"的 SDK 调用数，超出的立即 503，配合 try_acquire/release。
    线程安全：内部 threading.Lock，持锁时间极短（仅计数器增减）。
    """

    def __init__(self, max_concurrent: int = 5):
        self._max = max_concurrent
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._max:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
```

- [ ] **Step 5: 在 `create_app` 中初始化 SdkGate 并接入路由**

a. 在 `app/http_app.py` 的 import 区（约 23-26 行），将 errors 的 import 追加 `SERVICE_BUSY`：

```python
from app.errors import (
    AppError, INTERNAL_ERROR, INVALID_REQUEST, REALTIME_SUBSCRIPTION_FAILED,
    RequestIdMiddleware, SDK_NOT_READY, SDK_QUERY_FAILED, SERIALIZATION_FAILED,
    SERVICE_BUSY, get_request_id,
)
```

b. 在 `create_app` 内 `app.state.health_service = health_service` 之后追加（约 132 行）：

```python
    app.state.sdk_gate = SdkGate(max_concurrent=5)
```

c. 在 `/daily` 路由的 try 块开头（约 220 行 `data = await asyncio.to_thread` 之前）插入闸门：

```python
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            data = await asyncio.to_thread(
                kline_service.query, req.codes, req.start_time, req.end_time
            )
            return {"data": data}
        finally:
            app.state.sdk_gate.release()
```

注意：原 `data = await asyncio.to_thread(...)` 和 `return {"data": data}` 移入 `try` 块，`finally` 释放闸门。异常处理（`except AppError` 等）保持不变，但需确保它们在 `try/finally` 之外或正确嵌套。完整 `/daily` 路由替换为：

```python
    @app.post("/daily")
    async def daily(req: DailyRequest, request: Request):
        logger.info("request_id=%s /daily codes=%d %s..%s",
                    get_request_id(request), len(req.codes),
                    req.start_time or "(default)", req.end_time or "(default)")
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            data = await asyncio.to_thread(
                kline_service.query, req.codes, req.start_time, req.end_time
            )
            return {"data": data}
        except AppError:
            raise
        except ValueError as e:
            raise AppError(INVALID_REQUEST, str(e), 422)
        except GatewayNotReadyError as e:
            raise AppError(SDK_NOT_READY, str(e), 503)
        except GatewayQueryError as e:
            raise AppError(SDK_QUERY_FAILED, str(e), 502)
        except (TypeError, OverflowError) as e:
            if "serialize" in str(e).lower() or "json" in str(e).lower():
                raise AppError(SERIALIZATION_FAILED, str(e), 502)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        except Exception as e:
            logger.error("unhandled error: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()
```

d. 对 `/minute` 路由做同样改造（在 `try` 前加 `try_acquire`，`finally` 加 `release`）。完整 `/minute` 路由替换为：

```python
    @app.post("/minute")
    async def minute(req: MinuteRequest, request: Request):
        period = req.period or "min1"
        if period not in MINUTE_PERIODS:
            raise AppError(INVALID_REQUEST, f"unsupported period: {period}", 422)
        logger.info("request_id=%s /minute codes=%d period=%s %s..%s",
                    get_request_id(request), len(req.codes), period,
                    req.start_time or "(default)", req.end_time or "(default)")
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            data = await asyncio.to_thread(
                kline_service.query, req.codes, req.start_time, req.end_time, period=period
            )
            return {"data": data}
        except AppError:
            raise
        except ValueError as e:
            raise AppError(INVALID_REQUEST, str(e), 422)
        except GatewayNotReadyError as e:
            raise AppError(SDK_NOT_READY, str(e), 503)
        except GatewayQueryError as e:
            raise AppError(SDK_QUERY_FAILED, str(e), 502)
        except (TypeError, OverflowError) as e:
            if "serialize" in str(e).lower() or "json" in str(e).lower():
                raise AppError(SERIALIZATION_FAILED, str(e), 502)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        except Exception as e:
            logger.error("unhandled error: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()
```

- [ ] **Step 6: 运行 http_app 测试确认通过**

Run: `python -m pytest tests/test_http_app.py -v`
Expected: 全部 PASS（含原有 + 新增 2 个 SdkGate 测试）

- [ ] **Step 7: Commit**

```bash
git add app/errors.py app/http_app.py tests/test_http_app.py
git commit -m "feat: add SdkGate concurrency limiter with 503 fast-fail on overload"
```

---

## 最终验证

- [ ] **运行完整测试套件**

Run: `python -m pytest -v`
Expected: 全部 PASS（原 107 + 新增约 12 = ~119）

- [ ] **确认无 deprecation warning**

Run: `python -c "from app.http_app import create_app; import warnings; warnings.simplefilter('error'); create_app(config=__import__('app.config',fromlist=['Config']).Config(username='u',password='p',ip='1.2.3.4',port=1), gateway=__import__('tests.conftest',fromlist=['FakeGateway']).FakeGateway())"`
Expected: 无 DeprecationWarning（on_event 已迁移）

---

## Self-Review

**1. Spec coverage（P0-P2 项）：**
- P1 序列化向量化 → Task 1 ✓
- D1 lifespan 迁移 → Task 4 ✓
- D2 Dockerfile 读环境变量 → Task 5 ✓
- D3 SDK 自动重连 → Task 6（惰性重连，非后台心跳，因 SDK 无可靠探活调用）✓
- D4 时区显式化 → Task 3 ✓
- P3 fallback concat → Task 8 ✓
- P4 realtime 浅拷贝出锁 → Task 7 ✓
- P5 _snapshot_to_dict 类型缓存 → Task 9 ✓
- D5/D6 并发保护 → Task 10（SdkGate 快速失败 + 限制在飞数）✓
- P2 _flatten 浅拷贝 → Task 2 ✓

**2. Placeholder scan：** 无 TBD/TODO，所有步骤含完整代码。Task 5 的验证用 `docker compose config`（无 Python 测试，属配置改动）。

**3. Type consistency：**
- `SdkGate` 在 Task 10 定义，路由用 `app.state.sdk_gate.try_acquire()`/`release()`，测试用 `SdkGate(max_concurrent=...)` 一致 ✓
- `_is_connection_error` 在 Task 6 定义为模块级函数，测试 import 路径 `from app.gateway import _is_connection_error` 一致 ✓
- `SERVICE_BUSY` 在 `errors.py` 定义，`http_app.py` import，测试 import 一致 ✓
- `serialize_dataframe` 签名不变，Task 1 改内部实现，Task 8 仍调 `serialize_dataframe(merged)` 一致 ✓
- `_build_extract_fn` 在 Task 9 定义为 staticmethod，`_snapshot_to_dict` 调用 `self._build_extract_fn(data)` 一致 ✓
- `_extract_fns` 在 `__init__` 初始化为 `dict`，`_snapshot_to_dict` 用 `self._extract_fns.get(t)` 一致 ✓

**4. 依赖顺序：** Task 1（序列化）先于 Task 8（fallback 用序列化）；Task 4（lifespan）先于 Task 10（路由改造）；Task 2/3 改 kline_service 互不冲突；Task 7/8/9 改 realtime_service 不同方法，顺序执行。✓

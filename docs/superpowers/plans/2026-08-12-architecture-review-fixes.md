# 架构审查修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复架构审查确认的 12 项问题（#1~#10、#12、#13），不改变端点契约，测试全绿。

**Architecture:** 在现有分层（http_app → service → gateway Mixin → SDK）上做定点修复：配置层容错与默认值、realtime 缓存正确性与热路径、gateway 超时自愈与订阅入锁、etf_flow 缓存、http_app 样板重构与规模上限、时区显式化。

**Tech Stack:** Python 3.13/3.14, FastAPI, pandas, pytest（全 FakeGateway，无真实 SDK）。

## Global Constraints

- **禁止 git commit**（工作区规则：未明确授权不提交）。每个任务的"提交"步骤改为：运行该任务相关的语法检查（`python -m py_compile <改动文件>`，多文件合并一条命令）。
- **子代理禁止跑 pytest**。pytest 只允许主代理在全部任务完成后执行（最多 2 次终端：全量落盘一次 + 修复后复跑受影响文件一次）。
- 子代理工具禁令（prompt 中必须重申）：禁止 Select-String/findstr/grep/rg；禁止内置 search_content；禁止终端 git。文本搜索用 codespelunker.search（snippet_mode="grep", context=10）；符号定位用 codedb；读文件用内置 read_file；编辑用 replace_in_file。
- 端点请求/响应契约不变（docs/API.md 仅新增 422 场景与透明 gzip）。
- `tzdata` 已被 pandas 2.x 间接依赖（pandas>=2.0 install_requires 含 tzdata>=2022.7），本地 Windows 测试环境 ZoneInfo 可用。
- 设计稿：`docs/superpowers/specs/2026-08-12-architecture-review-fixes-design.md`（含 review 修正版，先读它再动手）。

## 任务依赖与并行批次

- **批次 1（5 个任务完全并行，文件不相交）**：T1 config / T2 realtime_service / T3 resilience / T4 gateway 其余 / T6 http_app
- **批次 2（依赖批次 1）**：T5 etf_flow（依赖 T1 的 `etf_flow_cache_ttl_sec` 配置字段）∥ T7 scheduler+health（依赖 T4 的 `calendar_set` 属性）

---

### Task 1: Config 容错 + 默认值 + 新配置项（#1 #10，含 #4 前置）

**Files:**
- Modify: `app/config.py`
- Modify: `pyproject.toml`（加 tzdata 显式依赖）
- Modify: `.env.example`、`README.md`（文档同步）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.sdk_max_concurrent` 默认值 2；`Config.etf_flow_cache_ttl_sec: int = 300`（T5 消费）；模块级 `_env_int(name: str, default: int) -> int`。

- [ ] **Step 1: 改 `app/config.py`**

头部 import 区加 `import logging`。在 `Config` 类之前加模块级函数：

```python
logger = logging.getLogger("amazingdata.config")


def _env_int(name: str, default: int) -> int:
    """读取 int 型环境变量。缺失/空串用默认值；非法值 warning 后回退默认值（不崩溃）。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法整数，使用默认值 %d", name, raw, default)
        return default
```

dataclass 字段两处改动：
- `sdk_max_concurrent: int = 5` → `sdk_max_concurrent: int = 2`，注释改为 `# SDK 最大并发调用数（SDK 调用全局串行，此值只决定排队深度），超出返回 503`
- 在 `stale_max_age_sec` 行后新增：`etf_flow_cache_ttl_sec: int = 300  # /etf/net_inflow 结果缓存 TTL（秒），份额 T+1 更新无 freshness 风险`

`from_env` 中 7 处 `int(os.environ.get(...))` 全部替换为 `_env_int`，并新增第 8 项：

```python
port=_env_int("AMAZINGDATA_PORT", 0),
http_port=_env_int("HTTP_PORT", 3021),
sdk_max_concurrent=_env_int("SDK_MAX_CONCURRENT", 2),
...
stale_threshold_sec=_env_int("STALE_THRESHOLD_SEC", 90),
watchdog_interval_sec=_env_int("WATCHDOG_INTERVAL_SEC", 60),
reconnect_max_interval_sec=_env_int("RECONNECT_MAX_INTERVAL_SEC", 300),
stale_max_age_sec=_env_int("STALE_MAX_AGE_SEC", 300),
etf_flow_cache_ttl_sec=_env_int("ETF_FLOW_CACHE_TTL_SEC", 300),
```

（`http_host` 等 str 字段保持原样。）

- [ ] **Step 2: `pyproject.toml` dependencies 追加** `"tzdata>=2024.1",`（注释：Windows 本地运行 zoneinfo 需要；pandas 已间接依赖，此处显式声明）

- [ ] **Step 3: `.env.example` 同步**：`# SDK_MAX_CONCURRENT=5` 改为 `# SDK_MAX_CONCURRENT=2`，注释"默认 5"改"默认 2（SDK 调用全局串行，此值只决定排队深度）"。文件末尾追加：

```ini
# ===== ETF 净流入结果缓存 =====
# /etf/net_inflow 查询结果缓存 TTL（秒）。份额数据 T+1 才更新，300s 无 freshness 风险。
#ETF_FLOW_CACHE_TTL_SEC=300
```

`README.md` 配置表：`| SDK_MAX_CONCURRENT | 否 | 5 |` 行默认值改 2 并补说明；表格追加一行 `| ETF_FLOW_CACHE_TTL_SEC | 否 | 300 | /etf/net_inflow 结果缓存 TTL（秒） |`。

- [ ] **Step 4: 更新 `tests/test_config.py`**

`test_config_default_sdk_max_concurrent` 断言 `== 5` 改 `== 2`。文件末尾新增：

```python
def test_config_malformed_int_env_falls_back(monkeypatch):
    """畸形 int 环境变量不崩溃，warning 后回退默认值。"""
    monkeypatch.setenv("AMAZINGDATA_PORT", "not-a-number")
    monkeypatch.setenv("SDK_MAX_CONCURRENT", "abc")
    cfg = Config.from_env()
    assert cfg.port == 0
    assert cfg.sdk_max_concurrent == 2


def test_config_etf_flow_cache_ttl(monkeypatch):
    monkeypatch.delenv("ETF_FLOW_CACHE_TTL_SEC", raising=False)
    assert Config.from_env().etf_flow_cache_ttl_sec == 300
    monkeypatch.setenv("ETF_FLOW_CACHE_TTL_SEC", "600")
    assert Config.from_env().etf_flow_cache_ttl_sec == 600
```

- [ ] **Step 5: 验收**（不跑 pytest）：`python -m py_compile app/config.py tests/test_config.py`

---

### Task 2: RealtimeService — fallback 缓存 codes 维度 + watchdog join + 浅拷贝提取 + 时区（#3 #6 #8 #5 部分）

**Files:**
- Modify: `app/realtime_service.py`
- Modify: `app/subscription_schedule.py`（新增 `now_cn()`，T7 也消费）
- Test: `tests/test_realtime_service.py`

**Interfaces:**
- Produces: `app.subscription_schedule.now_cn() -> datetime.datetime`（tz-aware Asia/Shanghai，T7 消费）。
- Produces: `RealtimeService._fallback_codes: set[str]`。

- [ ] **Step 1: `app/subscription_schedule.py` 顶部加**

```python
from zoneinfo import ZoneInfo

_CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime.datetime:
    """当前时间（Asia/Shanghai，tz-aware）。窗口判定统一用显式时区，不依赖系统 TZ。"""
    return datetime.datetime.now(_CN_TZ)
```

- [ ] **Step 2: `realtime_service.py` 改动 4 处**

① `__init__` 在 `self._fallback_time: float = 0` 后加：

```python
self._fallback_codes: set[str] = set()  # fallback 缓存覆盖的 codes（TTL 命中判定用）
```

② `fallback_snapshot` 的 TTL 命中条件（两处：fast-path 与 singleflight 双检）由

```python
if self._fallback_cache is not None and now - self._fallback_time <= FALLBACK_TTL:
```

改为

```python
if (self._fallback_cache is not None
        and now - self._fallback_time <= FALLBACK_TTL
        and set(codes) <= self._fallback_codes):
```

③ 缓存写入处（`self._fallback_cache = records` / `self._fallback_time = time.time()`）改为合并写：

```python
# 按 code 合并进缓存（新记录覆盖同 code 旧记录），避免异 codes 请求互相挤掉缓存。
# 写序：先 _fallback_cache 后 _fallback_codes（引用赋值原子；读侧误判 miss 仅多查一次）。
merged_records = {r.get("code"): r for r in (self._fallback_cache or [])}
for r in records:
    merged_records[r.get("code")] = r
self._fallback_cache = list(merged_records.values())
self._fallback_codes |= set(codes)
self._fallback_time = time.time()
```

（合并后 TTL 对旧条目一并续期，属可接受简化，注释说明。）

④ `_build_extract_fn` 的 dataclass 分支：

```python
if dataclasses.is_dataclass(data):
    # 浅拷贝字段提取：asdict 是递归深拷贝，全市场高频推送热路径开销大。
    # 前提：SDK Snapshot 字段全为标量/datetime（probe 样本为扁平结构），
    # 嵌套对象由 serialize_value 原样透传（若出现会在 JSON 层暴露，届时再处理）。
    return lambda d: {f.name: getattr(d, f.name) for f in dataclasses.fields(d)}
```

⑤ 时区：`_in_recovery_window` 与 `_watchdog_loop` 中的 `datetime.datetime.now()` 改为 `now_cn()`，顶部 import 区 `from app.subscription_schedule import is_subscription_window` 改为 `from app.subscription_schedule import is_subscription_window, now_cn`。

⑥ `stop_watchdog` 加 join：

```python
def stop_watchdog(self) -> None:
    """优雅停止 watchdog。set flag 后 join 旧线程，确保 start_watchdog 时旧线程必死（防双线程/旧参数复跑）。"""
    self._stop_flag.set()
    t = self._watchdog_thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=2)
```

- [ ] **Step 3: 新增测试**（追加到 `tests/test_realtime_service.py`，FakeGateway 用法参照现有用例；query_snapshot 返回 `pd.DataFrame` 的 fake 需自行构造，可用 `tests.conftest.make_daily_df` 或现场 new 一个含 code 列的 DataFrame）

```python
def test_fallback_cache_misses_uncovered_codes():
    """TTL 内请求缓存未覆盖的 codes 必须穿透查询，不得返回错误的空结果。"""
    # 构造 fake gateway：query_snapshot 按 codes 返回对应 DataFrame（含 code 列），记录调用次数
    # 第一次 fallback_snapshot(["000001.SZ"]) → 查询 1 次，返回 1 条
    # 第二次 fallback_snapshot(["600000.SH"])（TTL 内）→ 必须再查询 1 次（不得命中旧缓存返回 []）
    # 断言 gateway 调用次数 == 2，第二次结果含 600000.SH

def test_fallback_cache_merges_codes():
    """异 codes 先后查询后，缓存为并集；请求并集 codes 在 TTL 内不再查询。"""
    # 依次查 ["000001.SZ"]、["600000.SH"]，再查 ["000001.SZ", "600000.SH"]
    # 断言第三次无新 gateway 调用，返回 2 条

def test_stop_watchdog_joins_thread():
    """stop_watchdog 后旧线程必须已退出（join 生效）。"""
    # start_watchdog 后 stop_watchdog，断言 rt._watchdog_thread.is_alive() is False
```

（实现时把上述注释展开为完整可运行测试代码；`RealtimeService(gateway=fake)`，`fake.query_snapshot` 返回 `{code: df}`，df 需非空且序列化后含 code 字段——参照文件内既有 fallback 测试的构造方式。）

- [ ] **Step 4: 验收**：`python -m py_compile app/realtime_service.py app/subscription_schedule.py tests/test_realtime_service.py`

---

### Task 3: 超时自愈 — `_call_sdk_with_timeout` 实例方法化（#2）

**Files:**
- Modify: `app/gateway/resilience.py`
- Test: `tests/test_amazingdata_gateway.py`

**Interfaces:**
- Consumes: `self._schedule_reconnect(reason: str)`（TgwEventMixin，同实例可用）；`self._ready`。
- Produces: 实例方法 `_call_sdk_with_timeout(self, fn, timeout_sec: float, label: str)`。

- [ ] **Step 1: `app/gateway/resilience.py`**

去掉 `@staticmethod`，改为实例方法，超时分支重写（完整方法）：

```python
    def _call_sdk_with_timeout(self, fn, timeout_sec: float, label: str):
        """在独立 daemon 线程执行 SDK 同步调用，超时抛 GatewayQueryError。

        超时处置（防幽灵并发）：SDK 是无限期阻塞的 C 层调用，超时后幽灵线程仍在
        native 层执行。此处置 _ready=False（后续查询立即 503，不与幽灵线程并发进
        SDK）并触发 _schedule_reconnect 退避重建会话；幽灵线程在旧会话对象上自然
        终结（与断线重连场景等价）。
        异常消息用中文"超过 Ns 无响应"，刻意避开 _CONNECTION_KEYWORDS（timeout 等），
        防止上层 _is_connection_error 误匹配后在 _ready=False 状态下重复 _do_login+重试。
        fn 抛出的异常原样上抛（不包装），保持上层连接错误/损坏检测语义。
        """
        holder: dict = {}

        def _run() -> None:
            try:
                holder["result"] = fn()
            except Exception as e:  # noqa: BLE001 - SDK 异常需原样传递
                holder["error"] = e

        t = threading.Thread(target=_run, daemon=True, name=f"sdk-{label}")
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            self._ready = False
            try:
                self._schedule_reconnect(f"sdk call timeout: {label}")
            except Exception:  # 重连调度失败不掩盖原始超时错误
                pass
            raise GatewayQueryError(
                f"{label} 超过 {timeout_sec}s 无响应（SDK 线程已隔离为 daemon，会话重建中）"
            )
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")
```

`query_basedata.py` 3 处调用点已是 `self._call_sdk_with_timeout(...)` 形式，无需改动（核实确认即可）。

- [ ] **Step 2: 更新 `tests/test_amazingdata_gateway.py:233-250` 两个现有用例**

`test_call_sdk_with_timeout_raises_on_hang` / `test_call_sdk_with_timeout_passthrough_error` 当前以类方式 `AmazingDataGateway._call_sdk_with_timeout(fn, ...)` 调用，改为实例调用：

```python
gw = AmazingDataGateway.__new__(AmazingDataGateway)  # 绕过 __init__（避免建目录/依赖 Config）
gw._ready = True
gw._schedule_reconnect = lambda reason: None  # 桩掉重连调度
gw._call_sdk_with_timeout(fn, timeout_sec, label)
```

- [ ] **Step 3: 新增用例**

```python
def test_call_sdk_with_timeout_marks_not_ready_and_schedules_reconnect():
    """超时后：_ready 置 False、触发 _schedule_reconnect、消息不含连接关键词。"""
    gw = AmazingDataGateway.__new__(AmazingDataGateway)
    gw._ready = True
    calls = []
    gw._schedule_reconnect = lambda reason: calls.append(reason)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw._call_sdk_with_timeout(lambda: time.sleep(5), 0.1, "hang-test")
    assert gw._ready is False
    assert calls and "hang-test" in calls[0]
    msg = str(exc_info.value).lower()
    assert "timeout" not in msg and "timed out" not in msg  # 防 _is_connection_error 误匹配
```

（文件顶部若无 `import time`/`import pytest` 需补；`GatewayQueryError` 已有导入则复用。）

- [ ] **Step 4: 验收**：`python -m py_compile app/gateway/resilience.py tests/test_amazingdata_gateway.py`

---

### Task 4: Gateway — calendar_set + 订阅入锁（#13 核心 + #7）

**Files:**
- Modify: `app/gateway/__init__.py`（`__init__` 加字段）
- Modify: `app/gateway/session.py`（维护 `_calendar_set` + property）
- Modify: `app/gateway/base.py`（Protocol 声明）
- Modify: `app/gateway/subscription.py`（入锁）
- Modify: `tests/conftest.py`（FakeGateway 加 `calendar_set`）
- Modify: `tests/test_scheduler_calendar_refresh.py`（FakeGW 加 `calendar_set`）
- Test: `tests/test_gateway_interface.py`（新增断言）

**Interfaces:**
- Produces: `Gateway.calendar_set -> frozenset`（T7 消费；空日历时为 `frozenset()`，falsy，与 None 同语义走 weekday 兜底）。

- [ ] **Step 1: `app/gateway/__init__.py`** 在 `self._calendar = None` 行后加：

```python
self._calendar_set: frozenset = frozenset()  # calendar 的 set 形态（窗口判定 O(1) 成员检查）
```

- [ ] **Step 2: `app/gateway/session.py` 三处 + property**

`_do_login` 中 `self._calendar = calendar` 后加 `self._calendar_set = frozenset(calendar or [])`。
`_safe_logout` 的 `if clear_calendar:` 块改为：

```python
if clear_calendar:
    self._calendar = None
    self._calendar_set = frozenset()
```

`refresh_calendar` 中 `self._calendar = calendar` 后加 `self._calendar_set = frozenset(calendar or [])`。
`calendar` property 后新增：

```python
@property
def calendar_set(self) -> frozenset:
    """交易日历的 frozenset 形态（O(1) 成员检查）。空日历返回空 frozenset。"""
    return self._calendar_set
```

- [ ] **Step 3: `app/gateway/base.py` Protocol** 在 `calendar` property 声明后加：

```python
    @property
    def calendar_set(self) -> frozenset: ...
```

- [ ] **Step 4: `app/gateway/subscription.py` 入锁**

`start_snapshot_subscription`：保留方法开头的 ready 检查不动，其后的全部 SDK 操作（`from AmazingData...` import 起到 `_sub_thread.start()` 日志止）包进 `with self._sdk_lock():`。
`stop_subscription`：整个方法体包进 `with self._sdk_lock():`。
在两方法 docstring 补一句：`持 _sdk_lock 串行化（订阅 start/stop 与查询一样触碰 SDK 内部状态）；sub.run 回调线程不持此锁，无死锁路径。`

- [ ] **Step 5: `tests/conftest.py` FakeGateway 加 property**（放在 `calendar` property 后）：

```python
    @property
    def calendar_set(self) -> frozenset:
        return frozenset(self._calendar) if self._calendar else frozenset()
```

`tests/test_scheduler_calendar_refresh.py` FakeGW 同样位置加相同 property。

- [ ] **Step 6: `tests/test_gateway_interface.py` 末尾新增**

```python
def test_fake_gateway_has_calendar_set_property():
    """FakeGateway 暴露 calendar_set（Protocol @runtime_checkable 需要）。"""
    gw = FakeGateway(ready=True)
    assert gw.calendar_set == frozenset()
    gw2 = FakeGateway(ready=True, calendar=[20240102, 20240103])
    assert gw2.calendar_set == frozenset({20240102, 20240103})
    assert isinstance(gw2, Gateway)
```

锁路径验证测试（`_sdk_lock` 默认 30s 超时且默认值在函数定义时绑定、monkeypatch 模块常量无效，故用线程 + Event 方案）：

```python
def test_stop_subscription_acquires_sdk_lock():
    """预持 gateway._lock 时 stop_subscription 必须阻塞等锁（证明经过 _sdk_lock）。"""
    import threading
    from app.config import Config
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(Config(username="u", password="p", ip="1.2.3.4", port=1))
    entered = threading.Event()   # stop_subscription 已返回
    gw._lock.acquire()
    try:
        t = threading.Thread(
            target=lambda: (gw.stop_subscription(), entered.set()), daemon=True
        )
        t.start()
        t.join(timeout=1.0)
        assert not entered.is_set()  # 仍在等 _sdk_lock（未持有锁时会立即返回）
    finally:
        gw._lock.release()
    t.join(timeout=5)
    assert entered.is_set()  # 释放锁后正常返回
```

（`AmazingDataGateway(Config(...))` 构造会触发 `resolve_*_local_path` 建目录——既有 gateway 测试已接受此副作用，保持一致。）

- [ ] **Step 7: 验收**：`python -m py_compile app/gateway/__init__.py app/gateway/session.py app/gateway/base.py app/gateway/subscription.py tests/conftest.py tests/test_scheduler_calendar_refresh.py tests/test_gateway_interface.py`

---

### Task 5: EtfFlowService 缓存 + 时区（#4 #5 部分）

**Files:**
- Modify: `app/etf_flow_service.py`
- Modify: `app/http_app.py`（仅 `EtfFlowService(gateway)` 构造处传 TTL——**与 T6 同文件，实现时协调：本任务只改 `create_app` 里一行**）
- Test: `tests/test_etf_flow_service.py`（参照现有测试文件构造）

**Interfaces:**
- Consumes: `Config.etf_flow_cache_ttl_sec`（T1）；`now_cn()`（T2，可选）。
- Produces: `EtfFlowService(gateway, cache_ttl_sec: int = 300)`。

**前置确认**：先 `read_file app/etf_flow_service.py` 与 `ls tests/` 确认测试文件名与现有 fixture 风格，再动手。

- [ ] **Step 1: `EtfFlowService.__init__` 扩展**

```python
def __init__(self, gateway: Gateway, cache_ttl_sec: int = 300):
    self._gw = gateway
    self._cache_ttl_sec = cache_ttl_sec
    self._cache_lock = threading.Lock()
    # 宽基 ETF 清单按日缓存：(date_str, codes, name_map)
    self._list_cache: tuple[str, list[str], dict[str, str]] | None = None
    # 查询结果缓存：key=(start_time, end_time) → (cached_at, records)
    self._result_cache: dict[tuple, tuple[float, list[dict]]] = {}
```

（`import threading` 顶部补；类 docstring 补缓存语义一段。）

- [ ] **Step 2: `query` 方法接入缓存**

默认区间解析（`_now = _dt.now()` 改 `now_cn()`，`from app.subscription_schedule import now_cn`）之后、日期解析之后：

```python
cache_key = (start_time, end_time)
now_ts = time.monotonic()
with self._cache_lock:
    hit = self._result_cache.get(cache_key)
    if hit is not None and now_ts - hit[0] <= self._cache_ttl_sec:
        logger.info("etf_flow 结果缓存命中: key=%s records=%d", cache_key, len(hit[1]))
        return list(hit[1])
```

返回前（`return records` 处）写缓存：

```python
with self._cache_lock:
    if len(self._result_cache) >= 64:  # 防界：key 组合异常膨胀时整体清空
        self._result_cache.clear()
    self._result_cache[cache_key] = (time.monotonic(), records)
```

ETF 清单段（`etf_df = self._gw.get_code_info(...)` + `_filter_broad_based`）改为：

```python
today = now_cn().strftime("%Y-%m-%d")
with self._cache_lock:
    list_hit = self._list_cache if self._list_cache and self._list_cache[0] == today else None
if list_hit is not None:
    _, codes, name_map = list_hit
else:
    etf_df = self._gw.get_code_info(security_type="EXTRA_ETF")
    broad_based = self._filter_broad_based(etf_df)
    codes = [c for c, _ in broad_based]
    name_map = {c: n for c, n in broad_based}
    with self._cache_lock:
        self._list_cache = (today, codes, name_map)
```

（保留原有 `t0/t1/t2` 计时日志，未命中路径才计时；命中路径打 debug 日志即可。）

- [ ] **Step 3: `app/http_app.py` 构造处改一行**：`etf_flow_service = EtfFlowService(gateway)` → `etf_flow_service = EtfFlowService(gateway, cache_ttl_sec=config.etf_flow_cache_ttl_sec)`。**只改这一行，不动 http_app 其他任何内容（其余归 T6）。**

- [ ] **Step 4: 新增测试**

```python
def test_etf_flow_result_cache_hit_skips_gateway():
    """同参数第二次查询零 gateway 调用。"""
    # FakeGateway(code_info_result=..., fund_share_result=..., fund_nav_result=...)
    # svc.query("2024-01-01", "2024-01-31") 两次
    # 断言 fake.get_fund_share 调用次数 == 1（fund_share_calls 长度）

def test_etf_flow_list_cache_expires_next_day():
    """清单缓存按日失效（跨日重取）。"""
    # 用 monkeypatch 替换 etf_flow_service.now_cn 返回不同日期，断言 get_code_info 被调两次
```

（实现时展开为完整代码；fund_share/nav 的 DataFrame 构造参照现有 etf_flow 测试文件。）

- [ ] **Step 5: 验收**：`python -m py_compile app/etf_flow_service.py app/http_app.py tests/test_etf_flow_service.py`

---

### Task 6: http_app — 规模上限 + GZip + 样板重构 + 死代码（#9 #12 #13 部分）

**Files:**
- Modify: `app/http_app.py`
- Modify: `app/errors.py`（删 `Optional` import）
- Test: `tests/test_http_app.py`

**Interfaces:**
- Produces: 模块常量 `MAX_CODES = 500`；`async _run_sdk_endpoint(app, fn, *args, **kwargs) -> dict`。

**前置确认**：T5 会在 `create_app` 改一行（EtfFlowService 构造）。若两任务并行，T6 不得重写该行；以 read_file 最新内容为准做最小 diff。

- [ ] **Step 1: 删死代码**：`from app.errors import (...)` 中去掉 `REALTIME_SUBSCRIPTION_FAILED,`；`app/errors.py` 删 `from typing import Optional`。

- [ ] **Step 2: codes 上限**。`DailyRequest`/`MinuteRequest`/`AdjFactorRequest` 提取公共基类：

```python
MAX_CODES = 500  # 单请求 codes 上限，超出 422 提示分批（防大响应打爆内存）


class _CodesRequest(BaseModel):
    """含 codes 列表的请求基类：非空 + 上限校验。"""

    codes: list[str]

    @field_validator("codes")
    @classmethod
    def codes_valid(cls, v):
        if not v or len(v) == 0:
            raise ValueError("codes must be a non-empty array")
        if len(v) > MAX_CODES:
            raise ValueError(f"codes exceeds max allowed ({MAX_CODES}), please split into batches")
        return v
```

三个模型改为继承 `_CodesRequest`，各自只保留自己的额外字段（`start_time`/`end_time`/`period`），删除各自的 `codes_nonempty` validator 与 `codes` 字段重复声明。docstring 中原"codes 必须是非空数组"语义注释保留。

- [ ] **Step 3: GZip**。import 区加 `from starlette.middleware.gzip import GZipMiddleware`；在两行 `add_middleware` 之后追加：

```python
# GZip 最后 add = 最外层（insert(0) 语义），压缩位于传输最外层
app.add_middleware(GZipMiddleware, minimum_size=1024)
```

- [ ] **Step 4: 抽 `_run_sdk_endpoint`**（放在 `SdkGate` 类之后、`create_app` 之前，模块级）：

```python
async def _run_sdk_endpoint(app: FastAPI, fn, *args, **kwargs) -> dict:
    """SDK 查询端点公共执行路径：并发闸门 + 线程池卸载 + 统一异常映射。

    只做 gate/to_thread/异常映射；各端点的请求日志与参数前置校验留在端点函数内。
    异常映射：ValueError→422 / GatewayNotReadyError→503 / GatewayQueryError→502 /
    TypeError·OverflowError(serialize/json)→502 / 其余→500。
    """
    if not app.state.sdk_gate.try_acquire():
        raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
    try:
        data = await asyncio.to_thread(fn, *args, **kwargs)
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
        logger.error("未处理异常: %s: %s", type(e).__name__, e)
        raise AppError(INTERNAL_ERROR, str(e), 500)
    finally:
        app.state.sdk_gate.release()
```

- [ ] **Step 5: 四端点改写**。以 `/daily` 为例，改写后函数体为：

```python
@app.post("/daily")
async def daily(req: DailyRequest, request: Request):
    """日 K 查询。返回 {"data": [...]}，空结果也是 200 + {"data": []}。

    start_time / end_time 可选；未传时由 SDK 使用默认区间。
    日期格式校验与 start<=end 校验在 KlineService 内完成，ValueError 转 422。
    """
    logger.info("request_id=%s /daily codes=%d %s..%s",
                get_request_id(request), len(req.codes),
                req.start_time or "(default)", req.end_time or "(default)")
    return await _run_sdk_endpoint(
        app, kline_service.query, req.codes, req.start_time, req.end_time
    )
```

`/minute` 保留 period 默认值与白名单校验（在调用 helper 前），然后 `return await _run_sdk_endpoint(app, kline_service.query, req.codes, req.start_time, req.end_time, period=period)`。`/adj_factor` 与 `/etf/net_inflow` 同理（后者无 codes 日志字段，保持原日志格式）。**`/realtime` 不动。**

- [ ] **Step 6: 测试**。`tests/test_http_app.py` 末尾新增：

```python
def test_daily_codes_exceeds_max_returns_422():
    """codes 超过 MAX_CODES(500) 返回 422 并提示分批。"""
    from app.http_app import create_app, MAX_CODES
    from tests.conftest import FakeGateway
    from fastapi.testclient import TestClient
    from app.config import Config
    cfg = Config(username="u", password="p", ip="1.2.3.4", port=3021, auth_required=False)
    app = create_app(config=cfg, gateway=FakeGateway(ready=True))
    with TestClient(app) as client:
        resp = client.post("/daily", json={
            "codes": [f"{i:06d}.SZ" for i in range(MAX_CODES + 1)],
        })
    assert resp.status_code == 422


def test_daily_codes_at_max_accepted():
    """恰好 MAX_CODES 个 codes 通过校验（SDK 查询本身用 FakeGateway 返回空）。"""
    ...同上构造，codes 数量 = MAX_CODES，断言 status_code == 200
```

（构造方式参照文件内既有用例的 create_app/TestClient 用法，以其为准。）

- [ ] **Step 7: 验收**：`python -m py_compile app/http_app.py app/errors.py tests/test_http_app.py`

---

### Task 7: Scheduler/Health 时区 + calendar_set 消费（#5 剩余 + #13 消费侧）

**Files:**
- Modify: `app/subscription_scheduler.py`
- Modify: `app/health.py`
- Test: `tests/test_subscription_schedule.py`（或既有 scheduler/health 测试文件，先 ls tests/ 确认）

**Interfaces:**
- Consumes: `now_cn()`（T2）；`Gateway.calendar_set`（T4）。

- [ ] **Step 0: 排查 fake 影响面**：scheduler/health 改为读 `calendar_set` 后，tests/ 下所有自建 fake gateway（定义了 `calendar` property 的类）都必须同步补 `calendar_set` property，否则 AttributeError。用 codespelunker.search("def calendar", snippet_mode="grep", context=3) 在 tests/ 下全量排查（已知：`tests/conftest.py` FakeGateway、`tests/test_scheduler_calendar_refresh.py` FakeGW 由 T4 覆盖；若还有其他文件，本任务一并补上相同的 property）。

- [ ] **Step 1: `app/subscription_scheduler.py`**：`_tick` 中 `now = datetime.datetime.now()` → `now = now_cn()`；`cal = self._gw.calendar` → `cal = self._gw.calendar_set`（frozenset 空集 falsy，与 None 同走 weekday 兜底，语义不变；`_start_subscription(cal)` 内 `refresh_calendar` 返回 list 仍兼容——`is_subscription_window` 对 list/set 均可）。import 区加 `from app.subscription_schedule import is_subscription_window, now_cn`（替换原 import）。`import datetime` 若无其他使用则删除。

- [ ] **Step 2: `app/health.py`**：`_realtime_detail` 与 `is_ok` 中 `datetime.datetime.now()` → `now_cn()`；`cal = self._gw.calendar` → `cal = self._gw.calendar_set`（两处）。import 相应调整（`import datetime` 若无残留使用则删除，加 `from app.subscription_schedule import is_subscription_window, now_cn`——`is_subscription_window` 原已导入则合并）。

- [ ] **Step 3: 测试**。新增（或追加到既有文件）：

```python
def test_is_subscription_window_accepts_aware_datetime_and_set():
    """tz-aware now + frozenset calendar 与 naive+list 行为一致。"""
    from zoneinfo import ZoneInfo
    from app.subscription_schedule import is_subscription_window
    cal_list = [20260812]  # 2026-08-12 周三
    cal_set = frozenset(cal_list)
    naive = datetime.datetime(2026, 8, 12, 10, 0)
    aware = datetime.datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert is_subscription_window(naive, cal_list) == is_subscription_window(aware, cal_set)
    assert is_subscription_window(aware, cal_set) is True
    outside = datetime.datetime(2026, 8, 12, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert is_subscription_window(outside, cal_set) is False
```

- [ ] **Step 4: 验收**：`python -m py_compile app/subscription_scheduler.py app/health.py tests/test_subscription_schedule.py`

---

## 主代理收尾（所有任务完成后）

- [ ] 全量测试一次落盘：`python -m pytest -q > pytest-out.txt 2>&1`，读 `pytest-out.txt` 找 `passed/failed/error` 关键词
- [ ] 若有失败：修复后只复跑受影响文件（合并一条命令），随后删除 `pytest-out.txt`
- [ ] 抽查 diff 确认无端点契约变化

# Subscription Lifecycle Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement time-windowed subscription, stale-detection watchdog, and health-check upgrade to eliminate false-alive and reduce off-hours CPU by ~60%.

**Architecture:** Three components — (A) `is_subscription_window` gate in lifespan, (B) watchdog thread + `realtime_detail` in /health, (C) systemd timer restarts + docker resource limits. All configurable via env vars.

**Tech Stack:** Python 3.14, FastAPI, pytest, threading.Event, systemd timers, podman/docker-compose

## Global Constraints

- Python ≥3.13, frozen dataclass Config pattern (add fields with defaults)
- Gateway Protocol is `@runtime_checkable` — FakeGateway must satisfy it
- Tests use pytest + httpx TestClient; FakeGateway is no-op for subscription
- Existing tests must not break — FakeGateway defaults `calendar=None` so non-subscription tests see non-window behavior (is_ok returns True)
- Dockerfile base: python:3.14-slim, timezone Asia/Shanghai
- Logger namespace: `amazingdata.*`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/config.py` | Modify | Add 4 configurable fields (subscription_open/close, stale_threshold_sec, watchdog_interval_sec) |
| `app/subscription_schedule.py` | Create | `is_subscription_window()` + `parse_hhmm()` pure functions |
| `app/gateway.py` | Modify | Add `calendar` property to Protocol + AmazingDataGateway |
| `app/realtime_service.py` | Modify | Add watchdog, deactivation_reason, auto-recovery, stop_watchdog |
| `app/health.py` | Modify | Add `realtime_detail`, upgrade `is_ok()` for window-aware check |
| `app/http_app.py` | Modify | Lifespan: window gate + start_watchdog + stop_watchdog |
| `tests/conftest.py` | Modify | FakeGateway: add `calendar` property |
| `tests/test_config.py` | Modify | Test new config fields |
| `tests/test_subscription_schedule.py` | Create | Unit tests for is_subscription_window |
| `tests/test_gateway_interface.py` | Modify | Verify FakeGateway satisfies Protocol with calendar |
| `tests/test_realtime_service.py` | Modify | Watchdog tests |
| `tests/test_health.py` | Create | HealthService realtime_detail + is_ok tests |
| `tests/test_http_app.py` | Modify | Lifespan integration tests + make_test_app helper |
| `docker-compose.yml` | Modify | Add resource limits |
| `deploy/systemd/` | Create | systemd unit files for scheduled restart |

---

### Task 1: Config Extension

**Files:**
- Modify: `app/config.py:24` (add fields after `auth_required`)
- Modify: `app/config.py:29-41` (update `from_env`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.subscription_open: str`, `Config.subscription_close: str`, `Config.stale_threshold_sec: int`, `Config.watchdog_interval_sec: int`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config.py`:

```python
def test_config_default_subscription_window(monkeypatch):
    monkeypatch.delenv("SUBSCRIPTION_OPEN", raising=False)
    monkeypatch.delenv("SUBSCRIPTION_CLOSE", raising=False)
    cfg = Config.from_env()
    assert cfg.subscription_open == "09:00"
    assert cfg.subscription_close == "15:20"


def test_config_reads_subscription_window(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_OPEN", "08:55")
    monkeypatch.setenv("SUBSCRIPTION_CLOSE", "15:30")
    cfg = Config.from_env()
    assert cfg.subscription_open == "08:55"
    assert cfg.subscription_close == "15:30"


def test_config_default_stale_threshold(monkeypatch):
    monkeypatch.delenv("STALE_THRESHOLD_SEC", raising=False)
    cfg = Config.from_env()
    assert cfg.stale_threshold_sec == 90


def test_config_reads_stale_threshold(monkeypatch):
    monkeypatch.setenv("STALE_THRESHOLD_SEC", "120")
    cfg = Config.from_env()
    assert cfg.stale_threshold_sec == 120


def test_config_default_watchdog_interval(monkeypatch):
    monkeypatch.delenv("WATCHDOG_INTERVAL_SEC", raising=False)
    cfg = Config.from_env()
    assert cfg.watchdog_interval_sec == 60


def test_config_reads_watchdog_interval(monkeypatch):
    monkeypatch.setenv("WATCHDOG_INTERVAL_SEC", "30")
    cfg = Config.from_env()
    assert cfg.watchdog_interval_sec == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v -k "subscription_window or stale_threshold or watchdog_interval" 2>&1 | tail -20`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'subscription_open'`

- [ ] **Step 3: Add fields to Config dataclass**

In `app/config.py`, add after line 24 (`auth_required: bool = False`):

```python
    subscription_open: str = "09:00"       # 订阅窗口开始 HH:MM
    subscription_close: str = "15:20"      # 订阅窗口结束 HH:MM
    stale_threshold_sec: int = 90          # watchdog 失活阈值（秒）
    watchdog_interval_sec: int = 60        # watchdog 检查间隔（秒）
```

- [ ] **Step 4: Update from_env to read new env vars**

In `app/config.py`, add to the `from_env` return dict (after `auth_required=...` line):

```python
            subscription_open=os.environ.get("SUBSCRIPTION_OPEN", "09:00") or "09:00",
            subscription_close=os.environ.get("SUBSCRIPTION_CLOSE", "15:20") or "15:20",
            stale_threshold_sec=int(os.environ.get("STALE_THRESHOLD_SEC", "90") or "90"),
            watchdog_interval_sec=int(os.environ.get("WATCHDOG_INTERVAL_SEC", "60") or "60"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v 2>&1 | tail -20`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add subscription window and watchdog config fields"
```

---

### Task 2: Subscription Schedule Module

**Files:**
- Create: `app/subscription_schedule.py`
- Create: `tests/test_subscription_schedule.py`

**Interfaces:**
- Produces: `is_subscription_window(now: datetime, calendar: list[int] | None, open_time: str = "09:00", close_time: str = "15:20") -> bool`
- Produces: `parse_hhmm(s: str) -> datetime.time`

- [ ] **Step 1: Write failing tests**

Create `tests/test_subscription_schedule.py`:

```python
import datetime

from app.subscription_schedule import is_subscription_window, parse_hhmm


def test_parse_hhmm_normal():
    assert parse_hhmm("09:00") == datetime.time(9, 0)
    assert parse_hhmm("15:20") == datetime.time(15, 20)
    assert parse_hhmm("00:00") == datetime.time(0, 0)
    assert parse_hhmm("23:59") == datetime.time(23, 59)


def test_is_window_trading_day_within_window():
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, cal) is True


def test_is_window_trading_day_before_window():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 8, 59)
    assert is_subscription_window(now, cal) is False


def test_is_window_trading_day_after_window():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 15, 21)
    assert is_subscription_window(now, cal) is False


def test_is_window_boundary_open():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 9, 0)
    assert is_subscription_window(now, cal) is True


def test_is_window_boundary_close():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 15, 20)
    assert is_subscription_window(now, cal) is True


def test_is_window_non_trading_day():
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 4, 10, 30)  # not in calendar
    assert is_subscription_window(now, cal) is False


def test_is_window_calendar_none():
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, None) is False


def test_is_window_calendar_empty():
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, []) is False


def test_is_window_custom_times():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 8, 30)
    assert is_subscription_window(now, cal, open_time="08:00", close_time="16:00") is True
    assert is_subscription_window(now, cal) is False  # default 09:00


def test_is_window_wide_window_always_true_on_trading_day():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 0, 0)
    assert is_subscription_window(now, cal, open_time="00:00", close_time="23:59") is True
    now = datetime.datetime(2024, 1, 2, 23, 58)
    assert is_subscription_window(now, cal, open_time="00:00", close_time="23:59") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_subscription_schedule.py -v 2>&1 | tail -20`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.subscription_schedule'`

- [ ] **Step 3: Create subscription_schedule module**

Create `app/subscription_schedule.py`:

```python
"""订阅时段判定：决定是否启动 SDK 快照订阅。

非交易时段不持有订阅会话，CPU 降 ~60%（每日省 ~16 小时 × 1 核）。
窗口时间可通过 Config 的 subscription_open / subscription_close 配置。
"""

import datetime


def parse_hhmm(s: str) -> datetime.time:
    """'09:00' → datetime.time(9, 0)"""
    h, m = s.split(":")
    return datetime.time(int(h), int(m))


def is_subscription_window(
    now: datetime.datetime,
    calendar: list[int] | None,
    open_time: str = "09:00",
    close_time: str = "15:20",
) -> bool:
    """是否应启动订阅：交易日 且 在 [open_time, close_time] 窗口内。

    calendar 为 None 或空时返回 False（login 前 / 无日历数据）。
    """
    if not calendar:
        return False
    today_int = int(now.strftime("%Y%m%d"))
    if today_int not in calendar:
        return False
    t = now.time()
    return parse_hhmm(open_time) <= t <= parse_hhmm(close_time)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_subscription_schedule.py -v 2>&1 | tail -20`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/subscription_schedule.py tests/test_subscription_schedule.py
git commit -m "feat: add subscription schedule window module"
```

---

### Task 3: Gateway Calendar Property

**Files:**
- Modify: `app/gateway.py:57-81` (Protocol) and `app/gateway.py:99-117` (AmazingDataGateway)
- Modify: `tests/conftest.py:12-27` (FakeGateway)
- Modify: `tests/test_gateway_interface.py`

**Interfaces:**
- Produces: `Gateway.calendar` property → `list[int] | None`
- Produces: `AmazingDataGateway.calendar` property → `list[int] | None`
- Produces: `FakeGateway.calendar` property → `list[int] | None`

- [ ] **Step 1: Write failing test**

Append to `tests/test_gateway_interface.py`:

```python
def test_fake_gateway_has_calendar_property():
    """FakeGateway 必须暴露 calendar 属性以满足 Gateway Protocol。"""
    gw = FakeGateway(ready=True)
    assert hasattr(gw, "calendar")
    assert gw.calendar is None  # default None


def test_fake_gateway_calendar_injectable():
    gw = FakeGateway(ready=True, calendar=[20240102, 20240103])
    assert gw.calendar == [20240102, 20240103]


def test_fake_gateway_satisfies_protocol_with_calendar():
    gw = FakeGateway(ready=True)
    assert isinstance(gw, Gateway)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gateway_interface.py -v -k "calendar" 2>&1 | tail -20`
Expected: FAIL with `AttributeError: 'FakeGateway' object has no attribute 'calendar'`

- [ ] **Step 3: Add calendar to Gateway Protocol**

In `app/gateway.py`, add to the `Gateway` Protocol class (after `def get_adj_factor` line, before the closing):

```python
    @property
    def calendar(self) -> list[int] | None: ...
```

- [ ] **Step 4: Add calendar property to AmazingDataGateway**

In `app/gateway.py`, add to `AmazingDataGateway` class (after `is_ready` method, around line 208):

```python
    @property
    def calendar(self) -> list[int] | None:
        """交易日历 list[int]（login 后可用，logout 后为 None）。"""
        return self._calendar
```

- [ ] **Step 5: Add calendar to FakeGateway**

In `tests/conftest.py`, modify `FakeGateway.__init__`:

Change:
```python
    def __init__(self, ready: bool = True, result: dict[str, pd.DataFrame] | None = _UNSET,
                 adj_factor_result: pd.DataFrame | None = None):
```
To:
```python
    def __init__(self, ready: bool = True, result: dict[str, pd.DataFrame] | None = _UNSET,
                 adj_factor_result: pd.DataFrame | None = None,
                 calendar: list[int] | None = None):
```

Add after `self._sub_code_list = None`:
```python
        self._calendar = calendar
```

Add property method (after `is_ready` method):
```python
    @property
    def calendar(self) -> list[int] | None:
        return self._calendar
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_gateway_interface.py -v 2>&1 | tail -20`
Expected: all PASS

- [ ] **Step 7: Run full test suite to check no regressions**

Run: `python -m pytest tests/ -v 2>&1 | tail -30`
Expected: all PASS (FakeGateway.calendar defaults None, no existing code accesses it yet)

- [ ] **Step 8: Commit**

```bash
git add app/gateway.py tests/conftest.py tests/test_gateway_interface.py
git commit -m "feat: add calendar property to Gateway Protocol and FakeGateway"
```

---

### Task 4: RealtimeService Watchdog

**Files:**
- Modify: `app/realtime_service.py`
- Modify: `tests/test_realtime_service.py`

**Interfaces:**
- Consumes: `is_subscription_window` from `app.subscription_schedule`
- Produces: `RealtimeService.start_watchdog(calendar, stale_threshold_sec, watchdog_interval_sec, open_time, close_time)`
- Produces: `RealtimeService.stop_watchdog()`
- Produces: `RealtimeService.deactivation_reason() -> str | None`
- Produces: `RealtimeService.last_snapshot_ts() -> float`
- Modifies: `on_snapshot()` (auto-recovery), `on_subscription_error()` (reason), `set_active()` (reason clear)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_realtime_service.py`:

```python
# ---------------------------------------------------------------------------
# Watchdog 测试
# ---------------------------------------------------------------------------

def test_on_snapshot_auto_recovery():
    """订阅曾被标记 inactive 但收到数据 → 自动恢复 active。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(False)
    svc._deactivation_reason = "stale"
    assert svc.is_active() is False
    svc.on_snapshot(_snap(last=10.0))
    assert svc.is_active() is True
    assert svc.deactivation_reason() is None


def test_on_subscription_error_sets_reason():
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.on_subscription_error(Exception("test"))
    assert svc.is_active() is False
    assert svc.deactivation_reason() == "error"


def test_set_active_true_clears_reason():
    svc = RealtimeService(gateway=None)
    svc._deactivation_reason = "stale"
    svc.set_active(True)
    assert svc.deactivation_reason() is None


def test_deactivation_reason_default_none():
    svc = RealtimeService(gateway=None)
    assert svc.deactivation_reason() is None


def test_last_snapshot_ts_default_zero():
    svc = RealtimeService(gateway=None)
    assert svc.last_snapshot_ts() == 0.0


def test_on_snapshot_updates_timestamp():
    import time as _time
    svc = RealtimeService(gateway=None)
    t_before = _time.time()
    svc.on_snapshot(_snap(last=10.0))
    t_after = _time.time()
    assert t_before <= svc.last_snapshot_ts() <= t_after


def test_watchdog_stale_marks_inactive():
    """窗口期内超过 stale_threshold 无数据 → 标记 inactive + reason=stale。"""
    import datetime
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    # 模拟收到过数据但已过期
    svc._last_snapshot_ts = 0.0  # 先设为0
    svc.on_snapshot(_snap())  # 收到一次数据，设置 timestamp
    svc._last_snapshot_ts = time.time() - 200  # 回拨到200秒前（超过90s阈值）
    # 手动调用 watchdog 逻辑（不启动线程，直接调内部方法一次）
    cal = [int(datetime.datetime.now().strftime("%Y%m%d"))]
    svc._watchdog_loop(cal, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="00:00", close_time="23:59")
    assert svc.is_active() is False
    assert svc.deactivation_reason() == "stale"


def test_watchdog_first_data_timeout_marks_inactive():
    """订阅启动后 stale_threshold 内未收到任何数据 → 标记 inactive。"""
    import datetime
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc._last_snapshot_ts = 0.0  # 从未收到数据
    svc._watchdog_start_ts = time.time() - 200  # 启动200秒前（超过90s）
    cal = [int(datetime.datetime.now().strftime("%Y%m%d"))]
    svc._watchdog_loop(cal, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="00:00", close_time="23:59")
    assert svc.is_active() is False
    assert svc.deactivation_reason() == "stale"


def test_watchdog_non_window_does_not_trigger():
    """非窗口期不判 stale。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc._last_snapshot_ts = time.time() - 200  # 很久没数据
    cal = [20231231]  # 非今天的日期
    svc._watchdog_loop(cal, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="09:00", close_time="15:20")
    assert svc.is_active() is True  # 非窗口期，不触发


def test_watchdog_calendar_none_does_not_trigger():
    """calendar 为 None 时不触发（is_subscription_window 返回 False）。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc._last_snapshot_ts = time.time() - 200
    svc._watchdog_loop(None, stale_threshold_sec=90, watchdog_interval_sec=0,
                       open_time="00:00", close_time="23:59")
    assert svc.is_active() is True


def test_stop_watchdog_sets_stop_flag():
    """stop_watchdog 设置 Event，watchdog 线程应能退出。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.start_watchdog([20240102], stale_threshold_sec=999, watchdog_interval_sec=999,
                       open_time="00:00", close_time="23:59")
    svc.stop_watchdog()
    # Event 应被 set
    assert svc._stop_flag.is_set()
    # 等待线程退出
    if svc._watchdog_thread:
        svc._watchdog_thread.join(timeout=2)
        assert not svc._watchdog_thread.is_alive()


def test_start_watchdog_idempotent():
    """重复调用 start_watchdog 不启动多个线程。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(True)
    svc.start_watchdog([20240102], stale_threshold_sec=999, watchdog_interval_sec=999,
                       open_time="00:00", close_time="23:59")
    t1 = svc._watchdog_thread
    svc.start_watchdog([20240102], stale_threshold_sec=999, watchdog_interval_sec=999,
                       open_time="00:00", close_time="23:59")
    assert svc._watchdog_thread is t1  # 同一个线程对象
    svc.stop_watchdog()
    if t1:
        t1.join(timeout=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_realtime_service.py -v -k "watchdog or auto_recovery or deactivation or last_snapshot" 2>&1 | tail -30`
Expected: FAIL with `AttributeError: 'RealtimeService' object has no attribute 'deactivation_reason'`

- [ ] **Step 3: Add imports and new fields to RealtimeService**

In `app/realtime_service.py`, add import at top (after existing imports):

```python
import datetime

from app.subscription_schedule import is_subscription_window
```

In `RealtimeService.__init__`, add after `self._extract_fns: dict = {}`:

```python
        # Watchdog 相关字段
        self._last_snapshot_ts: float = 0.0
        self._deactivation_reason: str | None = None  # "stale" / "error" / None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_start_ts: float = 0.0
        self._stop_flag = threading.Event()
```

- [ ] **Step 4: Modify on_snapshot for auto-recovery**

In `app/realtime_service.py`, replace the `on_snapshot` method:

```python
    def on_snapshot(self, data) -> None:
        """订阅回调：Snapshot → dict → 缓存覆盖。异常吞掉，不影响订阅线程。"""
        try:
            record = self._snapshot_to_dict(data)
            code = record.get("code") if record else None
            if code:
                with self._lock:
                    self._cache[code] = record
            self._last_snapshot_ts = time.time()
            # 自动恢复：若订阅曾被标记 inactive 但数据又来了，说明已恢复
            if not self._active:
                self._active = True
                self._deactivation_reason = None
                logger.info("subscription recovered: data received, reactivating")
        except Exception as e:
            logger.warning("on_snapshot convert failed: %s: %s", type(e).__name__, e)
```

- [ ] **Step 5: Modify on_subscription_error and set_active**

Replace `on_subscription_error`:

```python
    def on_subscription_error(self, err=None) -> None:
        """订阅线程崩溃/异常退出回调：标记不活跃，/realtime 将返回 503。"""
        self._active = False
        self._deactivation_reason = "error"
        logger.error("realtime subscription deactivated due to error: %s", err)
```

Replace `set_active`:

```python
    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._deactivation_reason = None
```

- [ ] **Step 6: Add getter methods**

Add after `set_active`:

```python
    def deactivation_reason(self) -> str | None:
        """供 HealthService 区分 inactive_stale / inactive_not_started / inactive_error。"""
        return self._deactivation_reason

    def last_snapshot_ts(self) -> float:
        """最后一次收到快照数据的时间戳（0=从未收到）。"""
        return self._last_snapshot_ts
```

- [ ] **Step 7: Add start_watchdog, stop_watchdog, _watchdog_loop**

Add after `last_snapshot_ts`:

```python
    def start_watchdog(
        self,
        calendar: list[int],
        stale_threshold_sec: int = 90,
        watchdog_interval_sec: int = 60,
        open_time: str = "09:00",
        close_time: str = "15:20",
    ) -> None:
        """启动后台 watchdog 线程。lifespan 订阅启动后调用。"""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._stop_flag.clear()
        self._watchdog_start_ts = time.time()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            args=(calendar, stale_threshold_sec, watchdog_interval_sec, open_time, close_time),
            daemon=True, name="sub-watchdog",
        )
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        """优雅停止 watchdog。lifespan shutdown 时调用。"""
        self._stop_flag.set()

    def _watchdog_loop(
        self,
        calendar: list[int] | None,
        stale_threshold_sec: int,
        watchdog_interval_sec: int,
        open_time: str,
        close_time: str,
    ) -> None:
        """watchdog 主循环：每 watchdog_interval_sec 检查一次订阅存活状态。"""
        while self._active and not self._stop_flag.is_set():
            if self._stop_flag.wait(timeout=watchdog_interval_sec):
                break
            if not self._active:
                break
            now = datetime.datetime.now()
            if not is_subscription_window(now, calendar, open_time, close_time):
                continue
            if self._last_snapshot_ts == 0:
                # 从未收到数据：检查启动后是否超过阈值
                elapsed_since_start = time.time() - self._watchdog_start_ts
                if elapsed_since_start > stale_threshold_sec:
                    logger.error(
                        "subscription started but no data received for %.0fs, marking inactive",
                        elapsed_since_start,
                    )
                    self._active = False
                    self._deactivation_reason = "stale"
                    break
                continue
            elapsed = time.time() - self._last_snapshot_ts
            if elapsed > stale_threshold_sec:
                logger.error(
                    "subscription stale: no data for %.0fs during trading hours, marking inactive",
                    elapsed,
                )
                self._active = False
                self._deactivation_reason = "stale"
                break
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_realtime_service.py -v 2>&1 | tail -30`
Expected: all PASS

- [ ] **Step 9: Commit**

```bash
git add app/realtime_service.py tests/test_realtime_service.py
git commit -m "feat: add subscription watchdog with stale detection and auto-recovery"
```

---

### Task 5: HealthService Upgrade

**Files:**
- Modify: `app/health.py`
- Create: `tests/test_health.py`

**Interfaces:**
- Consumes: `is_subscription_window` from `app.subscription_schedule`
- Consumes: `Gateway.calendar`, `RealtimeService.is_active()`, `deactivation_reason()`, `last_snapshot_ts()`
- Produces: `HealthService._realtime_detail() -> str`
- Modifies: `HealthService.status()` (add `realtime_detail` field)
- Modifies: `HealthService.is_ok()` (window-aware 503 logic)

- [ ] **Step 1: Write failing tests**

Create `tests/test_health.py`:

```python
import datetime
from unittest.mock import MagicMock

from app.config import Config
from app.health import HealthService


def _make_config(**kwargs):
    defaults = dict(
        username="u", password="p", ip="1.2.3.4", port=3021,
        subscription_open="00:00", subscription_close="23:59",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _make_realtime_svc(active=False, reason=None, last_ts=0.0):
    svc = MagicMock()
    svc.is_active.return_value = active
    svc.deactivation_reason.return_value = reason
    svc.last_snapshot_ts.return_value = last_ts
    return svc


def test_realtime_detail_active():
    gw = MagicMock()
    gw.is_ready.return_value = True
    gw.calendar = [20240102]
    rt = _make_realtime_svc(active=True)
    hs = HealthService(_make_config(), gw, rt)
    assert hs._realtime_detail() == "active"


def test_realtime_detail_inactive_offhours():
    """非窗口期（calendar 不含今天）→ inactive_offhours。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    gw.calendar = [20231231]  # not today
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(), gw, rt)
    assert hs._realtime_detail() == "inactive_offhours"


def test_realtime_detail_inactive_stale():
    """窗口期内 inactive + reason=stale → inactive_stale。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw.calendar = [today]
    rt = _make_realtime_svc(active=False, reason="stale", last_ts=1000.0)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs._realtime_detail() == "inactive_stale"


def test_realtime_detail_inactive_error():
    """窗口期内 inactive + reason=error → inactive_error。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw.calendar = [today]
    rt = _make_realtime_svc(active=False, reason="error")
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs._realtime_detail() == "inactive_error"


def test_realtime_detail_inactive_not_started():
    """窗口期内 inactive + 无 reason + 无数据 → inactive_not_started。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw.calendar = [today]
    rt = _make_realtime_svc(active=False, reason=None, last_ts=0.0)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs._realtime_detail() == "inactive_not_started"


def test_is_ok_offhours_inactive_returns_true():
    """非窗口期 inactive → is_ok() True（不要求 realtime 活跃）。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    gw.calendar = [20231231]  # not today
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(), gw, rt)
    assert hs.is_ok() is True


def test_is_ok_window_active_returns_true():
    """窗口期内 active → is_ok() True。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw.calendar = [today]
    rt = _make_realtime_svc(active=True)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs.is_ok() is True


def test_is_ok_window_inactive_returns_false():
    """窗口期内 inactive → is_ok() False（触发 503）。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw.calendar = [today]
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs.is_ok() is False


def test_is_ok_calendar_none_returns_true():
    """calendar=None → 非窗口 → is_ok() True（只要 config+gateway OK）。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    gw.calendar = None
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(), gw, rt)
    assert hs.is_ok() is True


def test_is_ok_gateway_not_ready_returns_false():
    gw = MagicMock()
    gw.is_ready.return_value = False
    gw.calendar = None
    rt = _make_realtime_svc(active=True)
    hs = HealthService(_make_config(), gw, rt)
    assert hs.is_ok() is False


def test_status_includes_realtime_detail():
    """status() 返回 dict 应包含 realtime_detail 字段。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    gw.calendar = [20231231]
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(), gw, rt)
    status = hs.status()
    assert "realtime_detail" in status
    assert status["realtime_detail"] == "inactive_offhours"


def test_status_no_realtime_svc():
    """realtime_service=None 时 realtime_detail=unavailable。"""
    gw = MagicMock()
    gw.is_ready.return_value = True
    gw.calendar = None
    hs = HealthService(_make_config(), gw, None)
    status = hs.status()
    assert status["realtime_detail"] == "unavailable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py -v 2>&1 | tail -30`
Expected: FAIL with `AttributeError: 'HealthService' object has no attribute '_realtime_detail'`

- [ ] **Step 3: Rewrite HealthService**

Replace entire `app/health.py` content:

```python
"""健康检查服务：综合配置完整性、SDK 登录状态和订阅存活状态判断服务是否可用。

/health 路由返回 200（全部就绪）或 503（配置缺失/SDK 未登录/窗口期内订阅失活），
响应体不含密码或连接凭据，可安全暴露给 Docker healthcheck。
"""

import datetime

from app.gateway import Gateway
from app.subscription_schedule import is_subscription_window


class HealthService:
    def __init__(self, config, gateway: Gateway, realtime_service=None):
        self._config = config
        self._gw = gateway
        self._realtime_svc = realtime_service

    def _realtime_detail(self) -> str:
        """计算 realtime_detail 状态。"""
        rt_svc = self._realtime_svc
        if not rt_svc:
            return "unavailable"
        if rt_svc.is_active():
            return "active"
        # inactive 分情况
        now = datetime.datetime.now()
        cal = self._gw.calendar
        if not cal or not is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
        ):
            return "inactive_offhours"
        # 窗口期内 inactive
        reason = rt_svc.deactivation_reason()
        if reason == "stale":
            return "inactive_stale"
        if reason == "error":
            return "inactive_error"
        if rt_svc.last_snapshot_ts() == 0:
            return "inactive_not_started"
        return "inactive_stale"  # 有过数据但无明确 reason，按 stale 处理

    def status(self) -> dict:
        """返回健康状态详情。"""
        ready = self._config.is_configured() and self._gw.is_ready()
        rt_detail = self._realtime_detail()
        rt = rt_detail == "active"
        if not self._config.auth_required:
            auth_state = "disabled"
        elif self._config.is_auth_valid():
            auth_state = "configured"
        else:
            auth_state = "misconfigured"
        return {
            "status": "ok" if (ready and self.is_ok()) else "degraded",
            "sdk": "ready" if self._gw.is_ready() else "not_ready",
            "config": "complete" if self._config.is_configured() else "incomplete",
            "realtime": "active" if rt else "inactive",
            "realtime_detail": rt_detail,
            "auth": auth_state,
        }

    def is_ok(self) -> bool:
        """快捷判断：配置完整 + SDK 就绪 + 窗口期内订阅活跃。/health 据此返回 200 或 503。"""
        if not (self._config.is_configured() and self._gw.is_ready()):
            return False
        now = datetime.datetime.now()
        cal = self._gw.calendar
        if cal and is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
        ):
            return self._realtime_svc.is_active() if self._realtime_svc else False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_health.py -v 2>&1 | tail -30`
Expected: all PASS

- [ ] **Step 5: Run full suite to check regressions**

Run: `python -m pytest tests/ -v 2>&1 | tail -30`
Expected: all PASS (existing tests use FakeGateway with calendar=None → non-window → is_ok True)

- [ ] **Step 6: Commit**

```bash
git add app/health.py tests/test_health.py
git commit -m "feat: add realtime_detail and window-aware health check"
```

---

### Task 6: HTTP App Lifespan Integration

**Files:**
- Modify: `app/http_app.py:171-228` (lifespan function)
- Modify: `tests/test_http_app.py` (make_test_app + new tests)

**Interfaces:**
- Consumes: `is_subscription_window` from `app.subscription_schedule`
- Consumes: `Config.subscription_open/close`, `Config.stale_threshold_sec`, `Config.watchdog_interval_sec`
- Consumes: `Gateway.calendar`, `RealtimeService.start_watchdog()`, `RealtimeService.stop_watchdog()`

- [ ] **Step 1: Write failing tests**

In `tests/test_http_app.py`, first update `make_test_app` to support calendar injection:

Replace the existing `make_test_app`:
```python
def make_test_app(gateway=None, auth_token="", auth_required=False,
                  subscription_open="09:00", subscription_close="15:20"):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
        subscription_open=subscription_open, subscription_close=subscription_close,
    )
    if gateway is None:
        gateway = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)
```

Then append new tests:

```python
import datetime
from tests.conftest import FakeGateway


def _today_cal():
    return [int(datetime.datetime.now().strftime("%Y%m%d"))]


def test_lifespan_skips_subscription_outside_window():
    """非窗口期启动：不创建 subscription_thread，不调 start_snapshot_subscription。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="23:58", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        # subscription_thread 不应被创建
        assert not hasattr(app.state, "subscription_thread") or \
               app.state.subscription_thread is None
        assert gw.sub_start_called == 0
    assert gw.logout_called >= 1


def test_lifespan_starts_subscription_in_window():
    """窗口期启动：创建 subscription_thread，调用 start_snapshot_subscription。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        sub_thread = getattr(app.state, "subscription_thread", None)
        if sub_thread:
            sub_thread.join(timeout=5)
        assert gw.sub_start_called == 1
        assert app.state.realtime_service.is_active() is True


def test_lifespan_starts_watchdog_in_window():
    """窗口期启动订阅后应启动 watchdog 线程。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59",
                    watchdog_interval_sec=999)
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        sub_thread = getattr(app.state, "subscription_thread", None)
        if sub_thread:
            sub_thread.join(timeout=5)
        rt_svc = app.state.realtime_service
        assert rt_svc._watchdog_thread is not None
        assert rt_svc._watchdog_thread.is_alive()


def test_lifespan_skips_subscription_no_calendar():
    """calendar=None（gateway 未 login）时不启动订阅。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=None)
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        assert gw.sub_start_called == 0


def test_health_503_when_stale_in_window():
    """窗口期内订阅 stale → /health 返回 503。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        sub_thread = getattr(app.state, "subscription_thread", None)
        if sub_thread:
            sub_thread.join(timeout=5)
        # 手动标记 stale
        rt_svc = app.state.realtime_service
        rt_svc.set_active(False)
        rt_svc._deactivation_reason = "stale"
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["realtime_detail"] == "inactive_stale"


def test_health_200_offhours_inactive():
    """非窗口期 inactive → /health 返回 200 + realtime_detail=inactive_offhours。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="23:58", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["realtime_detail"] == "inactive_offhours"


def test_shutdown_stops_watchdog():
    """lifespan shutdown 应调用 stop_watchdog。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59",
                    watchdog_interval_sec=999)
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        sub_thread = getattr(app.state, "subscription_thread", None)
        if sub_thread:
            sub_thread.join(timeout=5)
        rt_svc = app.state.realtime_service
        assert rt_svc._watchdog_thread is not None
    # 退出 with 后 watchdog 应已停止
    assert rt_svc._stop_flag.is_set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_http_app.py -v -k "lifespan or shutdown_stops or health_503 or health_200_offhours" 2>&1 | tail -30`
Expected: FAIL (lifespan doesn't have window check yet)

- [ ] **Step 3: Add import to http_app.py**

In `app/http_app.py`, add after the existing imports (around line 34):

```python
from app.subscription_schedule import is_subscription_window
import datetime
```

- [ ] **Step 4: Modify lifespan startup for window gate**

In `app/http_app.py`, replace the `_init_subscription` function and subscription startup block inside lifespan.

Replace from `def _init_subscription():` through `app.state.subscription_thread.start()`:

```python
                def _init_subscription(cal):
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
                        realtime_service.start_watchdog(
                            cal,
                            stale_threshold_sec=config.stale_threshold_sec,
                            watchdog_interval_sec=config.watchdog_interval_sec,
                            open_time=config.subscription_open,
                            close_time=config.subscription_close,
                        )
                        t2 = time.monotonic()
                        logger.info(
                            "realtime subscription started: %d symbols "
                            "(get_realtime_code_list=%.3fs subscribe=%.3fs)",
                            len(code_list), t1 - t0, t2 - t1,
                        )
                    except Exception as e:
                        logger.error("realtime subscription start failed: %s: %s", type(e).__name__, e)
                cal = gateway.calendar
                if cal and is_subscription_window(
                    datetime.datetime.now(), cal,
                    open_time=config.subscription_open,
                    close_time=config.subscription_close,
                ):
                    app.state.subscription_thread = threading.Thread(
                        target=_init_subscription, args=(cal,), daemon=True, name="sub-init"
                    )
                    app.state.subscription_thread.start()
                else:
                    logger.info("outside subscription window, skipping subscription (SDK query still available)")
```

- [ ] **Step 5: Modify lifespan shutdown for stop_watchdog**

In `app/http_app.py`, replace the shutdown section. Add `realtime_service.stop_watchdog()` before the subscription thread join:

```python
        yield
        # shutdown
        realtime_service.stop_watchdog()
        sub_thread = getattr(app.state, "subscription_thread", None)
        if sub_thread and sub_thread.is_alive():
            sub_thread.join(timeout=10)
```

- [ ] **Step 6: Update existing subscription tests for window compatibility**

The existing tests `test_realtime_startup_activates_subscription`, `test_realtime_not_active_after_startup_failure`, and `test_realtime_startup_subscribes_combined_list_with_index` need calendar injection + wide window to ensure subscription starts regardless of test time.

For each of these tests, update the Config and FakeGateway creation. For example, in `test_realtime_startup_activates_subscription`:

Replace:
```python
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
```
With:
```python
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
```

Apply the same pattern to:
- `test_realtime_not_active_after_startup_failure`
- `test_realtime_startup_subscribes_combined_list_with_index`
- `test_shutdown_calls_gateway_logout`

Add `_today_cal()` helper at the top of the test file if not already added:
```python
import datetime

def _today_cal():
    return [int(datetime.datetime.now().strftime("%Y%m%d"))]
```

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -v 2>&1 | tail -40`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add app/http_app.py tests/test_http_app.py
git commit -m "feat: integrate subscription window gate and watchdog into lifespan"
```

---

### Task 7: Deployment Config

**Files:**
- Modify: `docker-compose.yml`
- Create: `deploy/systemd/amazing-data-http-restart-open.service`
- Create: `deploy/systemd/amazing-data-http-restart-open.timer`
- Create: `deploy/systemd/amazing-data-http-restart-close.service`
- Create: `deploy/systemd/amazing-data-http-restart-close.timer`
- Create: `deploy/systemd/amazing-data-http-watchdog.service`
- Create: `deploy/systemd/amazing-data-http-watchdog.timer`

- [ ] **Step 1: Add resource limits to docker-compose.yml**

In `docker-compose.yml`, add `deploy` section after `restart: unless-stopped`:

```yaml
    deploy:
      resources:
        limits:
          cpus: '1.5'
          memory: 2g
```

- [ ] **Step 2: Create systemd unit files**

Create `deploy/systemd/amazing-data-http-restart-open.service`:

```ini
[Unit]
Description=Restart amazing-data-http (enter subscription mode)

[Service]
Type=oneshot
ExecStart=/usr/bin/podman restart amazing-data-http
User=deployer
```

Create `deploy/systemd/amazing-data-http-restart-open.timer`:

```ini
[Unit]
Description=Restart amazing-data-http at market open

[Timer]
OnCalendar=Mon..Fri 08:55:00
Persistent=false

[Install]
WantedBy=timers.target
```

Create `deploy/systemd/amazing-data-http-restart-close.service`:

```ini
[Unit]
Description=Restart amazing-data-http (enter idle mode)

[Service]
Type=oneshot
ExecStart=/usr/bin/podman restart amazing-data-http
User=deployer
```

Create `deploy/systemd/amazing-data-http-restart-close.timer`:

```ini
[Unit]
Description=Restart amazing-data-http at market close

[Timer]
OnCalendar=Mon..Fri 15:25:00
Persistent=false

[Install]
WantedBy=timers.target
```

Create `deploy/systemd/amazing-data-http-watchdog.service` (external health watchdog for podman <4.4):

```ini
[Unit]
Description=Check amazing-data-http health and restart if unhealthy

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'status=$(podman inspect --format "{{.State.Health.Status}}" amazing-data-http 2>/dev/null); if [ "$status" = "unhealthy" ]; then podman restart amazing-data-http; fi'
User=deployer
```

Create `deploy/systemd/amazing-data-http-watchdog.timer`:

```ini
[Unit]
Description=Check amazing-data-http health every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml deploy/
git commit -m "feat: add resource limits and systemd timer deployment files"
```

---

## Self-Review

### Spec Coverage Check

| Design Section | Task |
|---|---|
| §4 Component A: Config extension | Task 1 |
| §4 Component A: Gateway calendar property | Task 3 |
| §4 Component A: is_subscription_window | Task 2 |
| §4 Component A: lifespan window gate | Task 6 |
| §5 Component B: RealtimeService watchdog | Task 4 |
| §5 Component B: HealthService realtime_detail + is_ok | Task 5 |
| §5 Component B: lifespan start/stop_watchdog | Task 6 |
| §6 Component C: systemd timers | Task 7 |
| §6 Component C: docker resource limits | Task 7 |
| §8 Tests: subscription_schedule | Task 2 |
| §8 Tests: watchdog | Task 4 |
| §8 Tests: health | Task 5 |
| §8 Tests: integration | Task 6 |

### Type Consistency Check

- `is_subscription_window(now, calendar, open_time, close_time)` — consistent across Tasks 2, 4, 5, 6 ✓
- `RealtimeService.start_watchdog(calendar, stale_threshold_sec, watchdog_interval_sec, open_time, close_time)` — consistent across Tasks 4, 6 ✓
- `RealtimeService.stop_watchdog()` — consistent across Tasks 4, 6 ✓
- `RealtimeService.deactivation_reason()` — consistent across Tasks 4, 5 ✓
- `RealtimeService.last_snapshot_ts()` — consistent across Tasks 4, 5 ✓
- `Gateway.calendar` property → `list[int] | None` — consistent across Tasks 3, 5, 6 ✓
- `Config.subscription_open/close/stale_threshold_sec/watchdog_interval_sec` — consistent across Tasks 1, 4, 5, 6 ✓

### Placeholder Scan

No TBD/TODO/placeholders found. All steps contain exact code.

### Existing Test Compatibility

- FakeGateway defaults `calendar=None` → existing non-subscription tests see non-window → `is_ok()` returns True (as before) ✓
- Existing subscription tests (`test_realtime_startup_*`) updated with `_today_cal()` + wide window ✓
- `test_health_ok` uses `make_test_app` without `with` → no lifespan → `is_ok()` checks calendar=None → non-window → True → 200 ✓

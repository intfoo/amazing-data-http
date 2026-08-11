# Gateway 稳定性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-08-11 生产日志暴露的 4 个问题：①交易日历过期导致当日日K静默返回空；②get_adj_factor 挂起10分钟+NoneType崩溃；③tgw断线后无主动重连；④scheduler/realtime 盘后每分钟死循环。

**Architecture:** 全部改动集中在 `app/` 层（gateway.py / realtime_service.py / subscription_scheduler.py / conftest.py），不改 HTTP 接口契约。核心机制：日历热刷新（`refresh_calendar`）、tgw 断线回调触发后台重连（`_schedule_reconnect`）、SDK 调用超时隔离（`_call_sdk_with_timeout`）、SDK 内部损坏检测重建（`_is_sdk_corruption`）、`on_snapshot` 窗口外不复活。

**Tech Stack:** Python 3.13 / FastAPI / pytest / AmazingData SDK（闭源 pyc，行为已通过字节码反汇编确认）

**背景（排查结论，实施者必读）：**
- SDK `query_kline` 先用 login 时快照的 `calendar` 本地过滤 `date_list`，日历不含查询日 → **零网络请求 0.000s 返回 {}**（生产 records=0 的真正原因，非"盘后未生成"）。`MarketData.calendar` 是普通属性，可热替换。
- SDK `query_kline` 异常路径**不释放内部 lock**（market_data.pyc 字节码证实：acquire 在入口、release 只在正常出口，无 try/finally）→ 一次 `Exception('查询失败')` 后 SDK 永久楔死。重建会话（`_do_login`）是唯一解法（新实例=新锁）。
- SDK 失败抛中文异常 `'查询失败'`，不在 `_CONNECTION_KEYWORDS` 里 → 现有惰性重连不触发。
- tgw 心跳超时每 30s 通过 `logged_on_log` 回调报告，当前只打 WARNING 不重连。
- 盘后 scheduler `stop_subscription` 不 join 订阅线程，SDK daemon 线程残留帧触发 `on_snapshot` 无条件复活 `_active`，与 scheduler 形成每分钟死循环。

## Global Constraints

- 日志/注释保持项目中文风格；logger 用模块级 `logger = logging.getLogger("amazingdata.xxx")`。
- 不改 HTTP 端点契约（状态码、响应结构不变）。
- 测试用 pytest + 既有 FakeGateway/fake 模式（`tests/conftest.py`）；真实 SDK 只在测试环境已安装 whl 时可用（既有 `test_query_kline_reconnects_on_connection_error` 已此前提）。
- 跑测试必须合并执行：`python -m pytest tests/test_xxx.py tests/test_yyy.py -q`；若终端输出被 PowerShell 吞掉，用 `> pytest-out.txt 2>&1` 落盘后读文件，读完删除临时文件。
- 每个 Task 的 commit 只 `git add` 本任务涉及的文件（工作区有大量无关未跟踪文件，严禁 `git add -A`）。
- 文中行号均为改动前锚点，编辑前必须先读文件确认。

---

### Task 1: realtime 窗口外不自动复活 + stop_subscription join 订阅线程（问题4）

**Files:**
- Modify: `app/realtime_service.py`（`__init__`:42-63、`on_snapshot`:65-80、`start_watchdog`:124-144）
- Modify: `app/gateway.py:654-664`（`stop_subscription`）
- Test: `tests/test_realtime_service.py`（追加）、`tests/test_amazingdata_gateway.py`（追加）

**Interfaces:**
- Produces: `RealtimeService.set_window_params(calendar: list[int] | None, open_time: str = "09:00", close_time: str = "15:20", calendar_fallback_weekday: bool = True) -> None` — `start_watchdog` 内部调用，测试可直接调用。
- Produces: `RealtimeService._in_recovery_window() -> bool` — 未设置窗口参数时返回 True（保持旧行为）。

- [ ] **Step 1: 写失败测试**

`tests/test_realtime_service.py` 末尾追加（该文件已有 `_snap(last=..., code=...)` helper，直接复用）：

```python
def test_on_snapshot_does_not_reactivate_outside_window():
    """窗口外收到残留帧：数据照收（last_snapshot_ts 更新），但不复活 _active。"""
    svc = RealtimeService(gateway=None)
    svc.set_window_params(calendar=None)  # calendar=None → is_subscription_window 恒 False
    svc.set_active(False)
    svc.on_snapshot(_snap(last=10.0, code="000001.SZ"))
    assert svc.is_active() is False
    assert svc.last_snapshot_ts() > 0


def test_on_snapshot_reactivates_inside_window():
    """窗口内恢复逻辑不变（盘中网络抖动恢复场景）。"""
    import datetime as _dt
    today = int(_dt.datetime.now().strftime("%Y%m%d"))
    svc = RealtimeService(gateway=None)
    svc.set_window_params(calendar=[today], open_time="00:00", close_time="23:59")
    svc.set_active(False)
    svc.on_snapshot(_snap(last=10.0, code="000001.SZ"))
    assert svc.is_active() is True


def test_on_snapshot_reactivates_when_no_window_params():
    """未设置窗口参数（订阅从未正式启动）：保持旧行为，允许自动复活。"""
    svc = RealtimeService(gateway=None)
    svc.set_active(False)
    svc.on_snapshot(_snap(last=10.0, code="000001.SZ"))
    assert svc.is_active() is True
```

`tests/test_amazingdata_gateway.py` 末尾追加：

```python
def test_stop_subscription_joins_thread():
    """stop_subscription 应调 stop() 并 join 订阅线程，防止残留帧触发回调。"""
    import threading
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    stop_flag = threading.Event()

    def _run():
        stop_flag.wait(timeout=10)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    class FakeSub:
        def stop(self):
            stop_flag.set()

    gw._subscribe_data = FakeSub()
    gw._sub_thread = t
    gw.stop_subscription()
    assert not t.is_alive()
    assert gw._subscribe_data is None
    assert gw._sub_thread is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_realtime_service.py tests/test_amazingdata_gateway.py -q`
Expected: 3 个新 realtime 测试 FAIL（`set_window_params` 不存在 AttributeError）；join 测试 PASS 或 FAIL 均可（join 尚未实现时 `t.is_alive()` 在 stop_flag.set() 后线程会自行退出，可能 PASS——此为加固测试，PASS 也接受，继续实现）。

- [ ] **Step 3: 实现 realtime_service.py 修改**

`app/realtime_service.py` `__init__` 末尾（`self._stop_flag = threading.Event()` 之后）追加：

```python
        # 订阅窗口参数（calendar, open_time, close_time, fallback），
        # 由 set_window_params/start_watchdog 设置，供 on_snapshot 判断窗口外残留帧不复活。
        self._window_params: tuple | None = None
```

`on_snapshot` 的自动恢复分支（现 65-80 行）替换为：

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
            # 自动恢复：仅窗口内恢复（盘中抖动场景）。窗口外收到的帧是退订残留帧
            # （stop_subscription 后 SDK daemon 线程未退出），复活会与调度器形成
            # 每分钟 stop→复活→stop 死循环。
            if not self._active:
                if self._in_recovery_window():
                    self._active = True
                    self._deactivation_reason = None
                    logger.info("订阅已恢复：收到数据，重新激活")
                else:
                    logger.debug("非窗口期收到数据帧，忽略自动恢复（退订残留帧）")
        except Exception as e:
            logger.warning("快照转换失败: %s: %s", type(e).__name__, e)
```

`set_active` 方法后插入两个新方法：

```python
    def set_window_params(
        self,
        calendar: list[int] | None,
        open_time: str = "09:00",
        close_time: str = "15:20",
        calendar_fallback_weekday: bool = True,
    ) -> None:
        """设置订阅窗口参数，供 on_snapshot 判断窗口外残留帧不自动复活。"""
        self._window_params = (calendar, open_time, close_time, calendar_fallback_weekday)

    def _in_recovery_window(self) -> bool:
        """on_snapshot 自动复活前检查当前是否在订阅窗口内。
        未设置窗口参数（订阅从未正式启动）时保持旧行为（允许恢复）。
        """
        if self._window_params is None:
            return True
        calendar, open_time, close_time, fallback = self._window_params
        return is_subscription_window(
            datetime.datetime.now(), calendar, open_time, close_time, fallback,
        )
```

`start_watchdog` 方法体第一行（docstring 之后、`if self._watchdog_thread ...` 之前）插入：

```python
        self.set_window_params(calendar, open_time, close_time, calendar_fallback_weekday)
```

- [ ] **Step 4: 实现 gateway.py stop_subscription join**

`app/gateway.py:654-664` 整个 `stop_subscription` 替换为：

```python
    def stop_subscription(self) -> None:
        """停止订阅。SDK 若有 stop() 则调用，随后 join 订阅线程防止残留帧触发回调。"""
        if self._subscribe_data is not None:
            try:
                stop = getattr(self._subscribe_data, "stop", None)
                if stop:
                    stop()
            except Exception as e:
                logger.warning("停止订阅异常（已忽略）: %s: %s", type(e).__name__, e)
        # join 订阅线程：sub.run() 是 daemon 无限循环，stop() 可能未真正退出线程。
        # 不 join 会导致退订后残留帧继续触发 on_snapshot（盘后误复活订阅）。
        thread = self._sub_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("订阅线程 5s 内未退出（SDK stop() 可能无效），依赖窗口检查兜底")
        self._subscribe_data = None
        self._sub_thread = None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_realtime_service.py tests/test_amazingdata_gateway.py -q`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add app/realtime_service.py app/gateway.py tests/test_realtime_service.py tests/test_amazingdata_gateway.py
git commit -m "fix(realtime): 窗口外残留帧不复活订阅 + stop_subscription join 订阅线程"
```

---

### Task 2: tgw 断线回调触发主动重连 + WARNING 去重（问题3）

**Files:**
- Modify: `app/gateway.py`（`__init__`:125-137、`_install_tgw_event_logger` 的 `logged_on_log`:299-325）
- Test: `tests/test_amazingdata_gateway.py`（追加）

**Interfaces:**
- Produces: `AmazingDataGateway._schedule_reconnect(reason: str) -> None` — 后台线程重连，防重入 + 60s 冷却。
- Produces: `AmazingDataGateway._should_log_disconnect(msg: str) -> bool` — 相同消息 300s 去重。

- [ ] **Step 1: 写失败测试**

`tests/test_amazingdata_gateway.py` 末尾追加：

```python
def test_schedule_reconnect_triggers_do_login(monkeypatch):
    """断线回调应触发后台线程重连，且防重入。"""
    import time as _time
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    calls = {"login": 0}
    monkeypatch.setattr(
        gw, "_do_login", lambda: calls.__setitem__("login", calls["login"] + 1)
    )
    gw._schedule_reconnect("test heartbeat timeout")
    for _ in range(50):
        if calls["login"] >= 1:
            break
        _time.sleep(0.05)
    assert calls["login"] == 1
    # 等重连线程收尾
    for _ in range(50):
        if not gw._reconnect_in_progress:
            break
        _time.sleep(0.05)
    assert gw._reconnect_in_progress is False


def test_schedule_reconnect_cooldown(monkeypatch):
    """冷却期内不重复重连（heartbeat 每 30s 报一次，不能每次都重连）。"""
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    monkeypatch.setattr(gw, "_do_login", lambda: None)
    gw._last_reconnect_attempt = 999999999999.0  # 刚尝试过 → 冷却中
    gw._schedule_reconnect("test")
    assert gw._reconnect_in_progress is False  # 未启动新线程


def test_disconnect_log_dedup():
    """相同断线消息 300s 内只打一次 WARNING。"""
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    assert gw._should_log_disconnect("heartbeat timeout") is True
    assert gw._should_log_disconnect("heartbeat timeout") is False
    assert gw._should_log_disconnect("another error") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 3 个新测试 FAIL（`_schedule_reconnect` / `_should_log_disconnect` 不存在）

- [ ] **Step 3: 实现**

`app/gateway.py` 模块级常量区（`_CONNECTION_KEYWORDS` 之后）追加：

```python
_RECONNECT_COOLDOWN_SEC = 60    # 主动重连冷却（heartbeat 每 30s 报一次，避免频繁 relogin）
_DISCONNECT_DEDUP_SEC = 300     # 相同断线 WARNING 去重窗口
```

`__init__` 末尾（`self._fund_local_path = ...` 之后）追加：

```python
        # 主动重连状态（tgw 断线回调触发，后台线程执行）
        self._reconnect_lock = threading.Lock()
        self._reconnect_in_progress = False
        self._last_reconnect_attempt = 0.0
        self._last_disconnect_log: dict = {"msg": None, "ts": 0.0}
```

`AmazingDataGateway` 类内新增两个方法（放在 `_install_tgw_event_logger` 之前）：

```python
    def _schedule_reconnect(self, reason: str) -> None:
        """tgw 断线回调触发主动重连：后台线程执行 _do_login()，不阻塞 tgw 回调线程。

        防重入（同时只有一个重连线程）+ 冷却（60s 内不重复尝试）。
        重连成功会顺带刷新交易日历（_do_login 重新 get_calendar）。
        """
        with self._reconnect_lock:
            if self._reconnect_in_progress:
                return
            now = time.time()
            if now - self._last_reconnect_attempt < _RECONNECT_COOLDOWN_SEC:
                return
            self._reconnect_in_progress = True
            self._last_reconnect_attempt = now

        def _do() -> None:
            try:
                logger.info("tgw 断线触发主动重连: %s", reason)
                with self._lock:
                    self._do_login()
                logger.info("tgw 主动重连成功")
            except Exception as e:
                logger.error("tgw 主动重连失败: %s: %s", type(e).__name__, e, exc_info=True)
            finally:
                with self._reconnect_lock:
                    self._reconnect_in_progress = False

        threading.Thread(target=_do, daemon=True, name="tgw-reconnect").start()

    def _should_log_disconnect(self, msg: str) -> bool:
        """断线 WARNING 去重：相同消息 _DISCONNECT_DEDUP_SEC 内只打一次。"""
        now = time.time()
        last = self._last_disconnect_log
        if msg == last["msg"] and now - last["ts"] < _DISCONNECT_DEDUP_SEC:
            return False
        last["msg"] = msg
        last["ts"] = now
        return True
```

`_install_tgw_event_logger` 内 `logged_on_log` 的 DISCONNECT 分支（现 311-312 行）替换为：

```python
            elif any(kw in msg_str for kw in DISCONNECT_KEYWORDS):
                if self._should_log_disconnect(msg_str):
                    logger.warning("tgw disconnect: [%s] %s", level_name, msg_str)
                # 断线主动重连：原设计只有惰性重连（需新请求触发），
                # 非交易时段无请求时断线可挂 1 小时不自愈。
                self._schedule_reconnect(msg_str)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/gateway.py tests/test_amazingdata_gateway.py
git commit -m "fix(gateway): tgw 断线回调触发主动重连（防重入+冷却），断线 WARNING 5 分钟去重"
```

---

### Task 3: 交易日历热刷新（问题1根因）

**Files:**
- Modify: `app/gateway.py`（Protocol `Gateway`:62-99、`query_kline`:546-600，新增 `refresh_calendar`）
- Modify: `app/subscription_scheduler.py:96-130`（`_start_subscription`）
- Modify: `tests/conftest.py:39-128`（`FakeGateway` 加 `refresh_calendar`）
- Test: `tests/test_amazingdata_gateway.py`（追加）、Create: `tests/test_scheduler_calendar_refresh.py`

**Interfaces:**
- Consumes: Task 1/2 不改本任务触碰的方法签名。
- Produces: `AmazingDataGateway.refresh_calendar() -> list[int]`；`Gateway` Protocol 与 `FakeGateway` 同步加 `refresh_calendar`。

- [ ] **Step 1: 写失败测试**

`tests/test_amazingdata_gateway.py` 末尾追加：

```python
def test_refresh_calendar_updates_market_data():
    """refresh_calendar 应重新拉取日历并热更新到 MarketData.calendar 属性。"""
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    gw._ready = True

    class FakeBase:
        def get_calendar(self):
            return [20240102, 20240103]

    class FakeMD:
        def __init__(self):
            self.calendar = [20240102]

    gw._base_data = FakeBase()
    gw._market_data = FakeMD()
    gw._calendar = [20240102]
    result = gw.refresh_calendar()
    assert result == [20240102, 20240103]
    assert gw._calendar == [20240102, 20240103]
    assert gw._market_data.calendar == [20240102, 20240103]


def test_query_kline_refreshes_stale_calendar():
    """end_date 超出日历最后一天时，query_kline 先刷新日历再查询。

    根因：SDK query_kline 用 login 时快照的 calendar 本地过滤 date_list，
    日历过期 → date_list 空 → 0.000s 静默返回空 dict（无网络请求、无异常）。
    """
    import pandas as pd
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    gw._ready = True
    gw._calendar = [20240102]

    class FakeBase:
        def get_calendar(self):
            return [20240102, 20240103]

    class FakeMD:
        def __init__(self):
            self.calendar = [20240102]

        def query_kline(self, codes, **kwargs):
            return {"000001.SZ": pd.DataFrame({"code": ["000001.SZ"], "close": [10.3]})}

    gw._base_data = FakeBase()
    gw._market_data = FakeMD()
    result = gw.query_kline(["000001.SZ"], 20240103, 20240103, "day")
    assert gw._calendar == [20240102, 20240103]
    assert "000001.SZ" in result
```

新建 `tests/test_scheduler_calendar_refresh.py`：

```python
"""调度器启动订阅前应热刷新交易日历（防 SDK 日历快照过期导致当日数据静默为空）。"""

from app.config import Config
from app.realtime_service import RealtimeService
from app.subscription_scheduler import SubscriptionScheduler


class FakeGW:
    def __init__(self):
        self._calendar = [20240101]
        self.refresh_called = 0

    @property
    def calendar(self):
        return self._calendar

    def refresh_calendar(self):
        self.refresh_called += 1
        self._calendar = [20240101, 20240102]
        return self._calendar

    def stop_subscription(self):
        pass

    def get_realtime_code_list(self):
        return ["000001.SZ"]

    def start_snapshot_subscription(self, code_list, on_data, on_error=None):
        pass


def make_config():
    return Config(username="u", password="p", ip="1.2.3.4", port=3021)


def test_start_subscription_refreshes_calendar():
    gw = FakeGW()
    rt = RealtimeService(gateway=None)
    sched = SubscriptionScheduler(gw, rt, make_config())
    sched._start_subscription(gw.calendar)
    try:
        assert gw.refresh_called == 1
        assert rt.is_active() is True
    finally:
        rt.stop_watchdog()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py tests/test_scheduler_calendar_refresh.py -q`
Expected: 新测试 FAIL（`refresh_calendar` 不存在）

- [ ] **Step 3: 实现 gateway 侧**

`Gateway` Protocol（`app/gateway.py:62-99`）在 `query_kline` 声明后加一行：

```python
    def refresh_calendar(self) -> list[int]: ...
```

`AmazingDataGateway` 新增方法（放在 `calendar` property 之后）：

```python
    def refresh_calendar(self) -> list[int]:
        """重新拉取交易日历并热更新到 MarketData.calendar 属性。

        根因背景：SDK query_kline 用 login 时快照的 calendar 本地过滤 date_list
        （market_data.pyc 字节码证实），日历不含查询日时 date_list 为空 →
        零网络请求 0.000s 静默返回 {}。长运行服务跨天后必须刷新日历。
        MarketData.calendar 是普通属性，直接赋值即热生效。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        with self._lock:
            calendar = self._base_data.get_calendar()
            self._calendar = calendar
            if self._market_data is not None:
                self._market_data.calendar = calendar
            logger.info(
                "交易日历已刷新: %d 天, 最新=%s",
                len(calendar), calendar[-1] if calendar else None,
            )
            return calendar
```

`query_kline` 的 ready 检查（现 560-561 行）之后、period 映射之前插入前置刷新：

```python
        # 日历过期防护：end_date 超出日历最后一天时先热刷新，否则 SDK 本地过滤后
        # date_list 为空，0.000s 静默返回空（无网络请求、无异常，极难排查）。
        if end_date is not None and self._calendar and end_date > self._calendar[-1]:
            logger.info(
                "query_kline end_date=%s 超出日历最后一天 %s，先刷新交易日历",
                end_date, self._calendar[-1],
            )
            self.refresh_calendar()
```

- [ ] **Step 4: 实现 scheduler 侧 + FakeGateway**

`app/subscription_scheduler.py` `_start_subscription`（96-130 行）`with self._action_lock:` 之后第一处插入：

```python
        with self._action_lock:
            # 交易日历热刷新：SDK 日历是 login 时快照，跨天后不含今天，
            # 会导致当日 K 线查询静默返回空。每天窗口开启启动订阅时顺带刷新。
            try:
                refreshed = self._gw.refresh_calendar()
                if refreshed:
                    cal = refreshed
            except Exception as e:
                logger.warning("调度器刷新交易日历失败（沿用旧日历）: %s: %s", type(e).__name__, e)
            # 先清理旧的订阅资源（防止重复订阅/线程泄漏）
            try:
                self._gw.stop_subscription()
            ...
```

（后续行保持不变，`cal` 被刷新值覆盖后传给 `start_watchdog`。）

`tests/conftest.py` `FakeGateway` 的 `logout` 方法后加：

```python
    def refresh_calendar(self):
        """FakeGateway 日历刷新：返回现有日历（测试用 set 赋值控制）。"""
        return self._calendar
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py tests/test_scheduler_calendar_refresh.py tests/test_http_app.py -q`
Expected: 全部 PASS（test_http_app 回归确认 FakeGateway 改动无副作用）

- [ ] **Step 6: Commit**

```bash
git add app/gateway.py app/subscription_scheduler.py tests/conftest.py tests/test_amazingdata_gateway.py tests/test_scheduler_calendar_refresh.py
git commit -m "fix(gateway): 交易日历热刷新——query_kline 前置检查 + 调度器启动订阅时刷新"
```

---

### Task 4: get_adj_factor 超时隔离 + None 防御 + is_local 回退（问题2）

**Files:**
- Modify: `app/gateway.py:666-705`（`get_adj_factor` 整体重写，新增 `_call_sdk_with_timeout`）
- Test: `tests/test_amazingdata_gateway.py`（追加）

**Interfaces:**
- Produces: `AmazingDataGateway._call_sdk_with_timeout(fn, timeout_sec: float, label: str)` — staticmethod，超时抛 `GatewayQueryError`，fn 异常原样上抛。
- Consumes: Task 5 会往本任务写的 except 块里再加 SDK 损坏重建分支。

- [ ] **Step 1: 写失败测试**

`tests/test_amazingdata_gateway.py` 末尾追加：

```python
def test_call_sdk_with_timeout_raises_on_hang():
    """SDK 调用超过 timeout 应抛 GatewayQueryError（线程隔离为 daemon）。"""
    import time as _t
    from app.gateway import AmazingDataGateway, GatewayQueryError

    with pytest.raises(GatewayQueryError, match="timed out"):
        AmazingDataGateway._call_sdk_with_timeout(lambda: _t.sleep(5), 0.1, "probe")


def test_call_sdk_with_timeout_passthrough_error():
    """fn 内部异常应原样上抛（不包装）。"""
    from app.gateway import AmazingDataGateway

    def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        AmazingDataGateway._call_sdk_with_timeout(_boom, 1, "probe")


def test_get_adj_factor_none_fallback_to_remote():
    """is_local=True 返回 None（本地缓存损坏）时回退 is_local=False 重试一次。"""
    import pandas as pd
    from app.config import Config
    from app.gateway import AmazingDataGateway

    cfg = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                 adj_factor_is_local=True)
    gw = AmazingDataGateway(cfg)
    gw._ready = True
    calls = []
    expected = pd.DataFrame({"000001.SZ": [1.0]})

    class FakeBase:
        def get_adj_factor(self, codes, local_path=None, is_local=False):
            calls.append(is_local)
            return None if is_local else expected

    gw._base_data = FakeBase()
    result = gw.get_adj_factor(["000001.SZ"])
    assert calls == [True, False]
    assert result is expected


def test_get_adj_factor_none_stays_none_raises():
    """本地+远程都返回 None 时显式报错（不再让下游拿到 None 炸 TypeError）。"""
    from app.config import Config
    from app.gateway import AmazingDataGateway, GatewayQueryError

    cfg = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                 adj_factor_is_local=True)
    gw = AmazingDataGateway(cfg)
    gw._ready = True

    class FakeBase:
        def get_adj_factor(self, codes, local_path=None, is_local=False):
            return None

    gw._base_data = FakeBase()
    with pytest.raises(GatewayQueryError, match="returned None"):
        gw.get_adj_factor(["000001.SZ"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 4 个新测试 FAIL

- [ ] **Step 3: 实现**

`app/gateway.py` 模块级常量区追加：

```python
ADJ_FACTOR_TIMEOUT_SEC = 120  # get_adj_factor SDK 调用超时（正常本地 <1s / 远程 ~21s）
```

`AmazingDataGateway` 新增 staticmethod（放在 `refresh_calendar` 之后）：

```python
    @staticmethod
    def _call_sdk_with_timeout(fn, timeout_sec: float, label: str):
        """在独立 daemon 线程执行 SDK 同步调用，超时抛 GatewayQueryError。

        SDK 是无限期阻塞的 C 层调用（无 timeout 参数），Python 无法真正中断线程，
        超时后 SDK 线程作为 daemon 隔离（不再持有 gateway._lock，不阻塞后续调用；
        极端情况下 SDK 内部可能仍有残留状态，由 _is_sdk_corruption 重建机制兜底）。
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
            raise GatewayQueryError(
                f"{label} timed out after {timeout_sec}s（SDK 线程已隔离为 daemon）"
            )
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")
```

`get_adj_factor`（现 666-705 行）`with self._lock:` 之后的方法体整体替换为：

```python
        with self._lock:
            try:
                result = self._call_sdk_with_timeout(
                    lambda: self._base_data.get_adj_factor(
                        codes,
                        local_path=self._adj_factor_local_path,
                        is_local=is_local,
                    ),
                    ADJ_FACTOR_TIMEOUT_SEC,
                    "get_adj_factor",
                )
            except Exception as e:
                logger.error("get_adj_factor 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local, exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_adj_factor 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._call_sdk_with_timeout(
                            lambda: self._base_data.get_adj_factor(
                                codes,
                                local_path=self._adj_factor_local_path,
                                is_local=is_local,
                            ),
                            ADJ_FACTOR_TIMEOUT_SEC,
                            "get_adj_factor",
                        )
                        logger.info("get_adj_factor 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_adj_factor 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_adj_factor failed after reconnect: {e2}") from e2
                raise GatewayQueryError(f"get_adj_factor failed: {e}") from e
            # SDK 内部状态损坏时可能静默返回 None（如本地 HDF5 缓存损坏，
            # 下游 df[...] 直接 TypeError）。is_local=True 时回退远程重试一次。
            if result is None:
                if is_local:
                    logger.warning(
                        "get_adj_factor is_local=True 返回 None（本地缓存可能损坏），"
                        "回退 is_local=False 远程重试"
                    )
                    result = self._call_sdk_with_timeout(
                        lambda: self._base_data.get_adj_factor(
                            codes,
                            local_path=self._adj_factor_local_path,
                            is_local=False,
                        ),
                        ADJ_FACTOR_TIMEOUT_SEC,
                        "get_adj_factor",
                    )
                if result is None:
                    raise GatewayQueryError(
                        "get_adj_factor returned None（SDK 内部错误，"
                        "建议删除本地 adj_factor 缓存后重试）"
                    )
            return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/gateway.py tests/test_amazingdata_gateway.py
git commit -m "fix(gateway): get_adj_factor 120s 超时隔离 + None 防御回退远程 + exc_info 完整栈"
```

---

### Task 5: SDK 内部状态损坏检测重建 + 异常日志补 exc_info（锁泄漏炸弹）

**Files:**
- Modify: `app/gateway.py`（模块常量区、`query_kline`:582-600、`query_snapshot`:520-533、`get_adj_factor`（Task 4 重写后）、`get_fund_share`:733-753、`get_fund_nav`:780-800）
- Test: `tests/test_amazingdata_gateway.py`（追加）

**Interfaces:**
- Produces: `_SDK_CORRUPTION_KEYWORDS: tuple[str, ...]`、`_is_sdk_corruption(exc: Exception) -> bool`（模块级函数）。
- Consumes: Task 4 重写后的 `get_adj_factor` except 块。

**背景：** SDK `MarketData.query_kline` 异常路径不释放内部 lock（字节码证实），一次 `Exception('查询失败')` 后 SDK 永久楔死；且 `'查询失败'` 是中文不匹配 `_CONNECTION_KEYWORDS`。本任务：命中损坏关键词 → `_do_login()` 重建会话（新 MarketData 实例=新锁）→ 不重试原查询（可能是合法失败），原异常照抛。

- [ ] **Step 1: 写失败测试**

`tests/test_amazingdata_gateway.py` 末尾追加：

```python
def test_is_sdk_corruption_keywords():
    from app.gateway import _is_sdk_corruption

    assert _is_sdk_corruption(Exception("查询失败"))
    assert _is_sdk_corruption(TypeError("'NoneType' object is not subscriptable"))
    assert not _is_sdk_corruption(ValueError("invalid code"))
    assert not _is_sdk_corruption(RuntimeError("Connection reset by peer"))


def test_query_kline_rebuilds_session_on_sdk_corruption(monkeypatch):
    """SDK 抛 '查询失败'（内部锁已泄漏）时应 _do_login 重建会话，然后原样报错。"""
    from app.gateway import AmazingDataGateway, GatewayQueryError

    gw = AmazingDataGateway(make_config())
    gw._ready = True
    calls = {"login": 0}

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            raise RuntimeError("查询失败")

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(
        gw, "_do_login", lambda: calls.__setitem__("login", calls["login"] + 1)
    )
    with pytest.raises(GatewayQueryError, match="query failed"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")
    assert calls["login"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 2 个新测试 FAIL

- [ ] **Step 3: 实现模块级常量 + 判断函数**

`app/gateway.py` `_CONNECTION_KEYWORDS` 定义之后追加：

```python
# SDK 内部状态损坏关键词（'查询失败' 是 SDK pyc 内硬编码的中文异常消息，
# 见 market_data.pyc 反汇编；'NoneType' 见于 get_code_list/get_adj_factor 内部状态错乱）。
# 命中说明 SDK 内部状态机/锁已损坏（SDK 异常路径不释放内部 lock），
# 必须 _do_login() 重建会话（新实例=新锁），否则后续所有查询永久挂起。
_SDK_CORRUPTION_KEYWORDS: tuple[str, ...] = ("查询失败", "NoneType")


def _is_sdk_corruption(exc: Exception) -> bool:
    """判断异常是否是 SDK 内部状态损坏（非网络问题，重试无意义，需重建会话）。"""
    msg = str(exc)
    return any(kw in msg for kw in _SDK_CORRUPTION_KEYWORDS)
```

- [ ] **Step 4: query_kline except 块替换**

`app/gateway.py:582-600` 整个 except 块替换为：

```python
            except Exception as e:
                logger.error(
                    "query_kline 失败: %s: %s (codes=%d, begin=%s, end=%s, period=%s)",
                    type(e).__name__, e, len(codes),
                    begin_date if begin_date is not None else "default",
                    end_date if end_date is not None else "default",
                    period,
                    exc_info=True,
                )
                if _is_connection_error(e):
                    logger.warning("query_kline 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_kline(codes, **kwargs)
                        logger.info("query_kline 重连后成功")
                        return result if isinstance(result, dict) else {"_all": result}
                    except Exception as e2:
                        logger.error("query_kline 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"query failed after reconnect: {e2}") from e2
                if _is_sdk_corruption(e):
                    # SDK 异常路径不释放内部 lock（market_data.pyc 字节码证实），
                    # 不重建会让后续所有查询在 SDK lock.acquire() 上永久挂起。
                    logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                raise GatewayQueryError(f"query failed: {e}") from e
```

- [ ] **Step 5: query_snapshot except 块替换**

`app/gateway.py:520-533` 整个 except 块替换为：

```python
            except Exception as e:
                logger.error("query_snapshot 失败: %s: %s (codes=%d, date=%s)",
                             type(e).__name__, e, len(codes), trade_date,
                             exc_info=True)
                if _is_connection_error(e):
                    logger.warning("query_snapshot 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_snapshot(codes, **kwargs)
                        logger.info("query_snapshot 重连后成功")
                    except Exception as e2:
                        logger.error("query_snapshot 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"query_snapshot failed after reconnect: {e2}") from e2
                else:
                    if _is_sdk_corruption(e):
                        logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                        try:
                            self._do_login()
                        except Exception as e3:
                            logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                    raise GatewayQueryError(f"query_snapshot failed: {e}") from e
```

- [ ] **Step 6: get_adj_factor except 块加损坏分支**

Task 4 重写后的 `get_adj_factor` except 块中，`if _is_connection_error(e):` 分支结束之后、`raise GatewayQueryError(f"get_adj_factor failed: {e}") from e` 之前插入：

```python
                if _is_sdk_corruption(e):
                    logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
```

- [ ] **Step 7: get_fund_share / get_fund_nav except 块同样处理**

`get_fund_share`（现 733-753）与 `get_fund_nav`（现 780-800）两个 except 块：
1. 三处 `logger.error(...)` 调用各加 `exc_info=True` 参数（每处 `logger.error` 的最后一个参数位置追加 `, exc_info=True`）；
2. `if _is_connection_error(e):` 分支结束之后、最终 `raise GatewayQueryError(...)` 之前插入以下分支（两个方法各插一次，完全相同）：

```python
                if _is_sdk_corruption(e):
                    logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
```

- [ ] **Step 8: 跑测试确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add app/gateway.py tests/test_amazingdata_gateway.py
git commit -m "fix(gateway): SDK 内部状态损坏（查询失败/NoneType）检测并重建会话，异常日志补完整栈"
```

---

### Task 6: gateway._lock 加竞争超时（兜底加固）

**Files:**
- Modify: `app/gateway.py`（全部 `with self._lock:` 站点）
- Test: `tests/test_amazingdata_gateway.py`（追加）

**Interfaces:**
- Produces: `AmazingDataGateway._sdk_lock(timeout_sec: float = SDK_LOCK_TIMEOUT_SEC)` — contextmanager，超时抛 `GatewayQueryError`。

**站点清单（改动前行号，编辑前重新定位）：** `login()`:202、`logout()`:369、`get_code_list`:405、`get_code_info`:435、`query_snapshot`:517、`query_kline`:578、`get_adj_factor`（Task 4 后）、`get_fund_share`:724、`get_fund_nav`:771、`refresh_calendar`（Task 3 新增）、`_schedule_reconnect._do`（Task 2 新增）。

- [ ] **Step 1: 写失败测试**

`tests/test_amazingdata_gateway.py` 末尾追加：

```python
def test_sdk_lock_timeout_raises():
    """_lock 被持有时 _sdk_lock 超时应抛 GatewayQueryError（不再无限排队）。"""
    from app.gateway import AmazingDataGateway, GatewayQueryError

    gw = AmazingDataGateway(make_config())
    assert gw._lock.acquire(blocking=False)
    try:
        with pytest.raises(GatewayQueryError, match="竞争超时"):
            with gw._sdk_lock(timeout_sec=0.1):
                pass
    finally:
        gw._lock.release()


def test_sdk_lock_normal_acquire_release():
    """无竞争时 _sdk_lock 正常进出并释放锁。"""
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(make_config())
    with gw._sdk_lock(timeout_sec=1):
        pass
    assert gw._lock.acquire(blocking=False)
    gw._lock.release()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_amazingdata_gateway.py -q`
Expected: 2 个新测试 FAIL（`_sdk_lock` 不存在）

- [ ] **Step 3: 实现**

`app/gateway.py` 顶部 import 区加：

```python
from contextlib import contextmanager
```

模块级常量区追加：

```python
SDK_LOCK_TIMEOUT_SEC = 30  # gateway._lock 竞争超时；超时说明有 SDK 调用挂起未释放
```

`AmazingDataGateway` 新增方法（放在 `_call_sdk_with_timeout` 之后）：

```python
    @contextmanager
    def _sdk_lock(self, timeout_sec: float = SDK_LOCK_TIMEOUT_SEC):
        """self._lock 的超时版本：挂起的 SDK 调用不再让后续请求无限排队假死。"""
        acquired = self._lock.acquire(timeout=timeout_sec)
        if not acquired:
            raise GatewayQueryError(
                f"gateway lock 竞争超时（{timeout_sec}s），存在挂起的 SDK 调用"
            )
        try:
            yield
        finally:
            self._lock.release()
```

将站点清单中所有 `with self._lock:` 替换为 `with self._sdk_lock():`（逐站点编辑，每处替换前后各读 5 行确认上下文唯一）。

- [ ] **Step 4: 跑全量测试回归**

Run: `python -m pytest tests/ -q`（输出若被吞则 `> pytest-out.txt 2>&1` 落盘读取，读完删文件）
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add app/gateway.py tests/test_amazingdata_gateway.py
git commit -m "feat(gateway): _lock 加 30s 竞争超时，挂起的 SDK 调用不再让服务假死"
```

---

## 部署后验证（非代码任务，不 commit）

1. 重启服务后观察日志：跨天（次日 09:00）调度器应打印"交易日历已刷新"；当日 `/daily` 应能查到当天数据（盘后）。
2. 断网模拟（可选）：拔掉 tgw 网络 30s+，应看到 `tgw disconnect` WARNING（仅一次）+ `tgw 断线触发主动重连`。
3. 盘后 15:20 后观察：不应再出现每分钟 `停止订阅`/`订阅已恢复` 循环日志。
4. 运维一次性操作：删除服务器上 `data/basedata/adj_factor/` 本地缓存（本次 TypeError 的损坏缓存），让 SDK 重建。

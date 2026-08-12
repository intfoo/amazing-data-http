# tgw 登录韧性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 根除 SDK login 失败 `SystemExit` 穿透导致的进程死亡/启动死循环，加重连退避、stale 缓存降级、登录失败诊断。

**Architecture:** 全部在应用层完成，不改 SDK。`except SystemExit` 显式兜底；tgw 事件钩子前置到 login 前（已验证 `import tgw` 后 `g_spi` 即存在，`interface.py` 模块级 `g_spi = TmpPushSpi()`）；wrap `AmazingData.login.tgw_login.set_cfg` 捕获 `log_spi` 提取失败类别。

**Tech Stack:** Python 3.13 / FastAPI / pytest / AmazingData SDK (pyc)

**Spec:** `docs/superpowers/specs/2026-08-12-tgw-login-resilience-design.md`

## Global Constraints

- 不改 SDK（whl/pyc）任何文件；monkey-patch 失败只允许 warning 降级，不得影响主流程。
- `Config` 是 frozen dataclass，新字段必须带默认值（既有测试按关键字构造）。
- 响应协议向后兼容：`/realtime` 新鲜数据响应仍为 `{"data": [...]}`，stale 字段仅 stale 时附加。
- 测试用 pytest + 既有 FakeGateway 模式（`tests/conftest.py`）。
- 终端纪律：pytest 每个任务只跑一次（合并该任务全部测试文件），禁止逐用例单独跑；最终集成由主会话统一跑全量。
- 禁止 Select-String/findstr；禁止内置 search_content；文本搜索用 codespelunker / codedb MCP。

---

### Task 1: Gateway 登录韧性核心（SystemExit 兜底 + 退避 + 诊断 + calendar 保留）

**Files:**
- Modify: `app/gateway/base.py`（常量 + Protocol 属性）
- Modify: `app/gateway/__init__.py`（实例字段）
- Modify: `app/gateway/session.py`（SystemExit 兜底、calendar 保留、钩子/probe 前置、last_login_error）
- Modify: `app/gateway/tgw_events.py`（退避、噪音过滤、事件缓冲、spi probe）
- Modify: `app/config.py`（两个新配置字段）
- Test: `tests/test_login_resilience.py`（新建）

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces（Task 2/3 依赖的名字）:
  - `Config.reconnect_max_interval_sec: int = 300`、`Config.stale_max_age_sec: int = 300`
  - Gateway Protocol 新只读属性：`last_login_error -> dict | None`、`reconnect_attempts -> int`
  - `AmazingDataGateway._reconnect_in_progress: bool`（已存在，Task 2 用 getattr 读）
  - `_safe_logout(clear_calendar: bool = False)`；`logout()` 传 `True`

- [ ] **Step 1: 写失败测试 `tests/test_login_resilience.py`**

```python
"""登录韧性：SystemExit 兜底 / calendar 保留 / 退避 / last_login_error。"""
from __future__ import annotations

import sys
import time
import types

import pytest

from app.config import Config
from app.gateway import AmazingDataGateway, GatewayNotReadyError


def _make_config() -> Config:
    return Config(username="u", password="p", ip="127.0.0.1", port=12345,
                  auth_required=False)


def _fake_ad_module(login_fn) -> types.ModuleType:
    m = types.ModuleType("AmazingData")
    m.login = login_fn
    return m


class TestSystemExitGuard:
    def test_login_exit0_becomes_not_ready(self, monkeypatch):
        """SDK login 内部 exit(0)（print 'login fail' 后）→ GatewayNotReadyError，进程存活。"""
        def fake_login(**kwargs):
            exit(0)  # 还原 SDK tgw_login.py:97 行为
        monkeypatch.setitem(sys.modules, "AmazingData", _fake_ad_module(fake_login))
        gw = AmazingDataGateway(_make_config())
        with pytest.raises(GatewayNotReadyError):
            gw.login()
        assert gw.is_ready() is False
        assert gw.last_login_error is not None
        assert gw.last_login_error["category"] in ("sdk_exit", "max_limitation")

    def test_calendar_preserved_on_failed_relogin(self, monkeypatch):
        """重连失败（SystemExit）后 calendar 保留，供调度器正确判定窗口。"""
        def fake_login(**kwargs):
            exit(0)
        monkeypatch.setitem(sys.modules, "AmazingData", _fake_ad_module(fake_login))
        gw = AmazingDataGateway(_make_config())
        gw._ready = True
        gw._calendar = [20260812]
        with pytest.raises(GatewayNotReadyError):
            gw.login()
        assert gw.is_ready() is False
        assert gw.calendar == [20260812]

    def test_logout_clears_calendar(self, monkeypatch):
        """显式 logout（shutdown 路径）仍清 calendar。"""
        def fake_login(**kwargs):
            exit(0)
        fake = _fake_ad_module(fake_login)
        fake.logout = lambda username: None
        monkeypatch.setitem(sys.modules, "AmazingData", fake)
        gw = AmazingDataGateway(_make_config())
        gw._ad = fake
        gw._ready = True
        gw._calendar = [20260812]
        gw.logout()
        assert gw.calendar is None


class TestReconnectBackoff:
    def _gw(self, monkeypatch):
        def fake_login(**kwargs):
            exit(0)
        monkeypatch.setitem(sys.modules, "AmazingData", _fake_ad_module(fake_login))
        gw = AmazingDataGateway(_make_config())
        gw._do_login = lambda: None  # 重连线程空调用，避免真实 login
        return gw

    def test_backoff_sequence(self, monkeypatch):
        """连续失败退避 60→120→240→300 封顶。"""
        gw = self._gw(monkeypatch)
        now = time.time()
        # failures=0 → 间隔 60s：61s 前尝试过 → 放行
        gw._reconnect_failures = 0
        gw._last_reconnect_attempt = now - 61
        gw._schedule_reconnect("test")
        assert gw._reconnect_in_progress is True
        gw._reconnect_in_progress = False
        # failures=3 → 间隔 min(60*8, 300)=300s：120s 前尝试 → 拦截
        gw._reconnect_failures = 3
        gw._last_reconnect_attempt = now - 120
        gw._schedule_reconnect("test")
        assert gw._reconnect_in_progress is False
        # 301s 前 → 放行
        gw._last_reconnect_attempt = now - 301
        gw._schedule_reconnect("test")
        assert gw._reconnect_in_progress is True

    def test_reconnect_attempts_counter(self, monkeypatch):
        gw = self._gw(monkeypatch)
        gw._last_reconnect_attempt = 0.0
        gw._schedule_reconnect("test")
        assert gw.reconnect_attempts == 1


class TestNoiseDedup:
    def test_independent_slot(self, monkeypatch):
        """噪音 dedup 独立槽位：60s 内自 dedup，且不压制断线 WARNING 槽位。"""
        def fake_login(**kwargs):
            exit(0)
        monkeypatch.setitem(sys.modules, "AmazingData", _fake_ad_module(fake_login))
        gw = AmazingDataGateway(_make_config())
        assert gw._should_log_noise("HandleFile | Now use ip <1.2.3.4>") is True
        assert gw._should_log_noise("HandleFile | Now use ip <1.2.3.4>") is False
        # 独立槽位：噪音记录不影响断线 WARNING dedup
        assert gw._should_log_disconnect("Push Heartbeat Check") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_login_resilience.py -q`
Expected: FAIL（`last_login_error` / `reconnect_attempts` 属性不存在）

- [ ] **Step 3: 实现 `app/config.py` 新字段**

dataclass 字段区追加（带默认值，位置在 `calendar_fallback_weekday` 之后）：

```python
    reconnect_max_interval_sec: int = 300  # tgw 主动重连退避上限（秒）
    stale_max_age_sec: int = 300           # /realtime 订阅缓存 stale 上限（秒），超过走 fallback
```

`from_env` 返回追加：

```python
            reconnect_max_interval_sec=int(os.environ.get("RECONNECT_MAX_INTERVAL_SEC", "300") or "300"),
            stale_max_age_sec=int(os.environ.get("STALE_MAX_AGE_SEC", "300") or "300"),
```

- [ ] **Step 4: 实现 `app/gateway/base.py`**

常量区追加：

```python
_RECONNECT_MAX_INTERVAL_SEC = 300   # 主动重连退避上限（Config 默认值同源）
_TGW_NOISE_PATTERNS: tuple[str, ...] = ("HandleFile", "Now use ip", "mdga.json")
_TGW_NOISE_DEDUP_SEC = 60           # tgw 噪音日志 dedup 窗口
```

`Gateway` Protocol 的 `calendar` property 后追加：

```python
    @property
    def last_login_error(self) -> dict | None: ...
    @property
    def reconnect_attempts(self) -> int: ...
```

- [ ] **Step 5: 实现 `app/gateway/__init__.py` 实例字段**

`self._last_disconnect_log` 行后追加：

```python
        self._last_noise_log: dict = {"msg": None, "ts": 0.0}  # tgw 噪音 dedup（独立槽位）
        self._reconnect_failures = 0    # 连续重连失败计数（指数退避用）
        self._reconnect_attempts = 0    # 累计重连尝试（/health 诊断）
        # 登录诊断（last_login_error / spi probe / 事件缓冲）
        self._last_login_error: dict | None = None
        self._last_login_spi = None     # set_cfg probe 捕获的 log_spi
        self._login_events: list[dict] = []  # 登录窗口事件环形缓冲（≤20 条）
        self._login_in_progress = False
```

- [ ] **Step 6: 实现 `app/gateway/session.py`**

顶部加 `import time`。`_do_login` 改造为（关键差异：钩子/probe 前置、`except SystemExit`、calendar 保留、last_login_error 构建、成功清诊断）：

```python
    def _do_login(self) -> None:
        """实际登录流程：import SDK → 装事件钩子/probe → login → BaseData → calendar → MarketData。

        SystemExit 兜底：SDK tgw_login.login 失败路径是 print('login fail') + exit(0)，
        SystemExit 是 BaseException，except Exception 接不住，必须显式捕获，
        否则穿透 lifespan 杀进程（uvicorn startup failed → 容器重启死循环）。
        """
        try:
            import AmazingData as ad
        except ImportError as e:
            logger.error("AmazingData SDK 导入失败: %s", e)
            self._ready = False
            raise GatewayNotReadyError(f"SDK import failed: {e}") from e

        self._ad = ad
        # 钩子/probe 前置：import tgw 后 g_spi 即存在（interface.py 模块级创建），
        # login 前安装可捕获失败全程的 OnLog/OnLogon 事件（真实失败原因）。
        self._install_tgw_event_logger()
        self._install_login_spi_probe()
        sdk_logged_in = False
        self._login_events.clear()
        self._login_in_progress = True
        try:
            if self._ready:
                self._safe_logout()
            ad.login(
                username=self._config.username,
                password=self._config.password,
                host=self._config.ip,
                port=self._config.port,
            )
            sdk_logged_in = True
            base = ad.BaseData()
            self._base_data = base
            calendar = base.get_calendar()
            self._calendar = calendar
            self._market_data = ad.MarketData(calendar)
            self._info_data = ad.InfoData()
            self._ready = True
            self._last_login_error = None
            logger.info("SDK 登录成功")
        except SystemExit as e:
            # SDK login 内部 exit(0) → 进程存活兜底
            self._build_last_login_error("sdk_exit", f"SDK login 内部 exit({e.code})")
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("SDK 登录失败: %s", self._last_login_error)
            raise GatewayNotReadyError(f"login failed: SDK exit({e.code})") from e
        except Exception as e:
            self._build_last_login_error("exception", f"{type(e).__name__}: {e}")
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("SDK 登录失败: %s", self._last_login_error)
            raise GatewayNotReadyError(f"login failed: {e}") from e
        finally:
            self._login_in_progress = False
```

SessionMixin 追加方法/属性（`_safe_logout` 改造 + 两个新成员）：

```python
    def _safe_logout(self, clear_calendar: bool = False) -> None:
        """登出并清理状态。登出异常被忽略（不影响后续重登录）。

        clear_calendar=False（重连路径默认）：保留 calendar（纯日期数据，当天有效），
        供调度器在重连失败期间正确判定订阅窗口，避免误判"不在窗口"杀订阅清缓存。
        """
        if self._ad is None:
            return
        try:
            self._ad.logout(username=self._config.username)
        except Exception as e:
            logger.warning("登出异常（已忽略）: %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None
        self._base_data = None
        self._info_data = None
        if clear_calendar:
            self._calendar = None

    def _build_last_login_error(self, category: str, detail: str) -> None:
        """构建登录失败诊断：spi max_limitation 升级分类 + 登录窗口事件缓冲。"""
        spi = self._last_login_spi
        if spi is not None and getattr(spi, "max_limitation", False):
            category = "max_limitation"
        self._last_login_error = {
            "ts": time.time(),
            "category": category,
            "detail": detail,
            "events": list(self._login_events)[-20:],
        }

    @property
    def last_login_error(self) -> dict | None:
        """最近一次登录失败诊断 {ts, category, detail, events}。成功登录后为 None。"""
        return self._last_login_error
```

`logout()` 改为传 `clear_calendar=True`：

```python
    def logout(self) -> None:
        """线程安全的登出入口。shutdown 路径：清空 calendar。"""
        with self._sdk_lock():
            self._safe_logout(clear_calendar=True)
```

删除 `_do_login` 成功路径末尾原有的 `self._install_tgw_event_logger()`（已前置）。

- [ ] **Step 7: 实现 `app/gateway/tgw_events.py`**

import 行改为从 base 多导入 `_TGW_NOISE_PATTERNS, _TGW_NOISE_DEDUP_SEC`。

`_schedule_reconnect` 改指数退避：

```python
    def _schedule_reconnect(self, reason: str) -> None:
        """tgw 断线回调触发主动重连：后台线程执行 _do_login()，不阻塞 tgw 回调线程。

        指数退避：60→120→240→reconnect_max_interval_sec(默认300) 封顶，成功复位。
        故障期降低 ad.login 调用频率（每次失败 native 层疑似泄漏资源，OOM 防护）。
        """
        with self._reconnect_lock:
            if self._reconnect_in_progress:
                return
            now = time.time()
            interval = min(
                _RECONNECT_COOLDOWN_SEC * (2 ** self._reconnect_failures),
                self._config.reconnect_max_interval_sec,
            )
            if now - self._last_reconnect_attempt < interval:
                return
            self._reconnect_in_progress = True
            self._last_reconnect_attempt = now
            self._reconnect_attempts += 1

        def _do() -> None:
            try:
                logger.info("tgw 断线触发主动重连: %s", reason)
                with self._sdk_lock():
                    self._do_login()
                with self._reconnect_lock:
                    self._reconnect_failures = 0
                logger.info("tgw 主动重连成功")
            except Exception as e:
                with self._reconnect_lock:
                    self._reconnect_failures += 1
                err = self._last_login_error or {}
                logger.error(
                    "tgw 主动重连失败: %s: %s (category=%s)",
                    type(e).__name__, e, err.get("category", "unknown"),
                )
            finally:
                with self._reconnect_lock:
                    self._reconnect_in_progress = False

        threading.Thread(target=_do, daemon=True, name="tgw-reconnect").start()
```

新增噪音 dedup + spi probe + reconnect_attempts 属性：

```python
    def _should_log_noise(self, msg: str) -> bool:
        """噪音日志 dedup：独立槽位（_last_noise_log），不与断线 WARNING 互相压制。"""
        with self._reconnect_lock:
            now = time.time()
            last = self._last_noise_log
            if msg == last["msg"] and now - last["ts"] < _TGW_NOISE_DEDUP_SEC:
                return False
            last["msg"] = msg
            last["ts"] = now
            return True

    def _install_login_spi_probe(self) -> None:
        """wrap AmazingData.login.tgw_login.set_cfg，捕获 log_spi 供失败分类。

        set_cfg 是模块级函数，login() 内经 LOAD_GLOBAL 运行时解析 → patch 模块属性生效。
        任何失败仅 warning 降级，不影响登录主流程。
        """
        try:
            from AmazingData.login import tgw_login
        except ImportError:
            return
        if getattr(tgw_login, "_spi_probe_installed", False):
            return
        original_set_cfg = tgw_login.set_cfg
        gateway = self

        def probed_set_cfg(*args, **kwargs):
            result = original_set_cfg(*args, **kwargs)
            try:
                gateway._last_login_spi = result[2]  # (cfg, api_mode, log_spi)
            except Exception:
                pass
            return result

        tgw_login.set_cfg = probed_set_cfg
        tgw_login._spi_probe_installed = True
        logger.info("tgw login spi probe installed on set_cfg")

    @property
    def reconnect_attempts(self) -> int:
        """累计主动重连次数（/health 诊断）。"""
        return self._reconnect_attempts
```

`logged_on_log` 改造（事件缓冲 + 噪音过滤）。`elif level == 3:` 分支改为：

```python
            elif level == 3:  # kError
                if any(p in msg_str for p in _TGW_NOISE_PATTERNS):
                    # tgw native 重连试 IP 等信息性消息被 SDK 错标 kError，降级 dedup
                    if self._should_log_noise(msg_str):
                        logger.info("tgw noise: [%s] %s", level_name, msg_str)
                elif "queue size" in msg_str or "in queue" in msg_str:
                    logger.debug("tgw push status: [%s] %s", level_name, msg_str)
                else:
                    logger.error("tgw error: [%s] %s", level_name, msg_str)
                    sys.stderr.flush()
```

`logged_on_log` 开头（`msg_str = ...` 之后）与 `logged_on_logon` 开头分别加事件缓冲：

```python
            # 登录窗口事件缓冲：login 失败时随 last_login_error 输出真实原因
            if self._login_in_progress:
                self._login_events.append(
                    {"kind": "log", "level": level_name, "msg": msg_str[:500]}
                )
                del self._login_events[:-20]
```

```python
            if self._login_in_progress:
                self._login_events.append({"kind": "logon", "msg": info[:500]})
                del self._login_events[:-20]
```

（`logged_on_logon` 中插在 `info` 组装完之后、logger.warning 之前。）

- [ ] **Step 8: 跑测试确认通过**

Run: `python -m pytest tests/test_login_resilience.py -q`
Expected: 6 passed

- [ ] **Step 9: Commit**

```bash
git add app/gateway/ app/config.py tests/test_login_resilience.py
git commit -m "feat(gateway): SystemExit 兜底 + 重连指数退避 + 登录失败诊断 + calendar 保留"
```

---

### Task 2: 订阅窗口兜底 + 调度器自愈 login

**Files:**
- Modify: `app/subscription_schedule.py`
- Modify: `app/subscription_scheduler.py`
- Test: `tests/test_subscription_schedule.py`（若已存在则追加，否则新建）、调度器测试追加到既有 scheduler 测试文件（先 `dir tests/` 找 `test_*schedul*`）

**Interfaces:**
- Consumes: `Gateway.is_ready()`（Protocol 已有）；`gateway.login()`；`_reconnect_in_progress`（getattr 防御读）；`Config.is_configured()`。
- Produces: `is_subscription_window` calendar=None 时 weekday 兜底（尊重 `calendar_fallback_weekday=False` → False）。

- [ ] **Step 1: 写失败测试**

`tests/test_subscription_schedule.py`（存在则追加 class，注意先读现有文件保持风格）：

```python
import datetime
from app.subscription_schedule import is_subscription_window

class TestNoneCalendarFallback:
    def test_none_calendar_weekday_in_window(self):
        """calendar=None（登录前/重连失败）+ 周三 14:00 → True（weekday 兜底）。"""
        wed = datetime.datetime(2026, 8, 12, 14, 0)  # 周三
        assert is_subscription_window(wed, None) is True

    def test_none_calendar_weekend(self):
        sat = datetime.datetime(2026, 8, 15, 14, 0)  # 周六
        assert is_subscription_window(sat, None) is False

    def test_none_calendar_out_of_hours(self):
        wed_evening = datetime.datetime(2026, 8, 12, 16, 0)  # 周三出窗
        assert is_subscription_window(wed_evening, None) is False

    def test_none_calendar_strict_mode(self):
        """calendar_fallback_weekday=False + calendar=None → False（严格模式）。"""
        wed = datetime.datetime(2026, 8, 12, 14, 0)
        assert is_subscription_window(wed, None, calendar_fallback_weekday=False) is False

    def test_cross_day_stale_calendar(self):
        """跨日残留 calendar（不含今天）+ 工作日 → weekday 兜底 True。"""
        wed = datetime.datetime(2026, 8, 12, 14, 0)
        assert is_subscription_window(wed, [20260811]) is True
```

调度器自愈测试（追加到既有 scheduler 测试文件，沿用其 FakeGW/fixture 风格；若既有 FakeGW 无 `is_ready`/`login`/`_reconnect_in_progress` 需补齐）：

```python
class TestSchedulerSelfHeal:
    def test_not_ready_triggers_login(self, ...):
        """窗口内 + not ready + 无重连进行中 → 调 gateway.login()，不启动订阅。"""
        # gateway: ready=False, login 成功置 ready；断言 login_called==1 且 sub_start_called==0

    def test_not_ready_skips_when_reconnect_in_progress(self, ...):
        """_reconnect_in_progress=True → 跳过 login（避免 _sdk_lock 竞争）。"""
        # 断言 login_called==0

    def test_login_failure_swallowed(self, ...):
        """login 抛异常被吞（debug 日志），_tick 不炸、进程不死。"""
        # gateway.login 抛 GatewayNotReadyError；_tick() 正常返回
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_subscription_schedule.py <既有scheduler测试文件> -q`
Expected: 新用例 FAIL

- [ ] **Step 3: 实现 `app/subscription_schedule.py`**

`is_subscription_window` 的 `if not calendar:` 分支改为：

```python
    if not calendar:
        # calendar 为 None（登录前/重连失败保留期）：weekday 兜底，
        # 避免日历缺失导致盘中误判"不在窗口"杀订阅清缓存（2026-08-12 事故）。
        if not calendar_fallback_weekday:
            return False
        if now.weekday() >= 5:
            return False
        t = now.time()
        return parse_hhmm(open_time) <= t <= parse_hhmm(close_time)
```

docstring 中 "calendar 为 None 或空时返回 False（login 前 / 无日历数据）" 改为 "calendar 为 None/空时走 weekday 兜底（calendar_fallback_weekday=False 时返回 False）"。

- [ ] **Step 4: 实现 `app/subscription_scheduler.py` `_tick`**

```python
    def _tick(self) -> None:
        """单次调度检查。"""
        now = datetime.datetime.now()
        cal = self._gw.calendar
        in_window = is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
            calendar_fallback_weekday=self._config.calendar_fallback_weekday,
        )
        if in_window:
            if not self._rt.is_active():
                if not self._gw.is_ready():
                    self._self_heal_login()
                    return
                logger.info("调度器：在订阅窗口内但订阅未活跃，尝试启动")
                self._start_subscription(cal)
        else:
            if self._rt.is_active():
                logger.info("调度器：不在订阅窗口，停止订阅")
                self._stop_subscription()

    def _self_heal_login(self) -> None:
        """启动失败自愈：not ready 时每 tick（60s 天然限频）重试 login。

        tgw 重连进行中跳过（避免 _sdk_lock 30s 竞争超时产生误导日志）。
        异常吞掉（含 GatewayQueryError 锁竞争超时）：下次 tick 再试，进程不死。
        """
        if getattr(self._gw, "_reconnect_in_progress", False):
            logger.debug("tgw 重连进行中，跳过调度器自愈 login")
            return
        if not self._config.is_configured():
            return
        try:
            self._gw.login()
            logger.info("调度器自愈 login 成功")
        except Exception as e:
            logger.debug("调度器自愈 login 失败: %s: %s", type(e).__name__, e)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_subscription_schedule.py <既有scheduler测试文件> -q`
Expected: 全部 PASS（含既有用例无回归）

- [ ] **Step 6: Commit**

```bash
git add app/subscription_schedule.py app/subscription_scheduler.py tests/
git commit -m "feat(scheduler): calendar None weekday 兜底 + not-ready 自愈 login"
```

---

### Task 3: /realtime stale 降级 + /health 诊断 + API 文档

**Files:**
- Modify: `app/realtime_service.py`（clear_cache 重置 + cache_age_sec）
- Modify: `app/http_app.py`（/realtime stale 响应 + 上限走 fallback）
- Modify: `app/health.py`（诊断字段）
- Modify: `tests/conftest.py`（FakeGateway 补 `last_login_error`/`reconnect_attempts`）
- Modify: `docs/API.md`（/realtime stale 字段说明）
- Test: `tests/test_realtime_stale.py`（新建）

**Interfaces:**
- Consumes: `Config.stale_max_age_sec`、`Config.stale_threshold_sec`（Task 1/既有）；Gateway `last_login_error`/`reconnect_attempts`（Task 1）。
- Produces: `RealtimeService.cache_age_sec -> float | None`；`/realtime` stale 响应字段。

- [ ] **Step 1: 写失败测试 `tests/test_realtime_stale.py`**

先读既有 http 测试（如 `tests/test_http_app.py` 或类似）复用其 app 构造模式（`create_app(config, gateway)` + TestClient + auth 关闭的 Config）。测试代码：

```python
"""stale 降级：缓存按年龄三态（新鲜/stale/过旧走 fallback）。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from app.realtime_service import RealtimeService
from tests.conftest import FakeGateway


def _make_app():
    config = Config(username="u", password="p", ip="1.2.3.4", port=1,
                    auth_required=False, stale_threshold_sec=90,
                    stale_max_age_sec=300)
    gw = FakeGateway(ready=True)
    app = create_app(config, gw)
    return app


def _seed_cache(app, age_sec: float):
    rt: RealtimeService = app.state.realtime_service
    rt._cache["000001.SZ"] = {"code": "000001.SZ", "last": 10.5,
                              "security_type": "stock"}
    rt._last_snapshot_ts = time.time() - age_sec


class TestRealtimeStale:
    def test_fresh_cache_no_stale_field(self):
        app = _make_app()
        with TestClient(app) as client:
            _seed_cache(app, 10)
            r = client.get("/realtime")
        assert r.status_code == 200
        body = r.json()
        assert body["data"] and "stale" not in body

    def test_stale_cache_marked(self):
        app = _make_app()
        with TestClient(app) as client:
            _seed_cache(app, 120)  # 90 < 120 <= 300
            r = client.get("/realtime")
        body = r.json()
        assert body["stale"] is True
        assert body["cache_age_sec"] >= 120
        assert body["data"]

    def test_too_old_cache_falls_back(self):
        """age > stale_max_age_sec → 放弃缓存走 fallback（无 codes + 有旧缓存语义不变）。"""
        app = _make_app()
        with TestClient(app) as client:
            _seed_cache(app, 400)
            r = client.get("/realtime")
        body = r.json()
        assert "stale" not in body  # 走 fallback（无 codes 返回 [] 或旧 fallback 缓存）

    def test_cache_age_sec_none_when_never_received(self):
        app = _make_app()
        rt: RealtimeService = app.state.realtime_service
        assert rt.cache_age_sec is None

    def test_clear_cache_resets_ts(self):
        app = _make_app()
        rt: RealtimeService = app.state.realtime_service
        rt._last_snapshot_ts = time.time()
        rt.clear_cache()
        assert rt.cache_age_sec is None


class TestHealthDiagnostics:
    def test_health_has_login_diag_fields(self):
        app = _make_app()
        with TestClient(app) as client:
            r = client.get("/health")
        body = r.json()
        assert "last_login_error" in body
        assert "reconnect_attempts" in body
        assert "cache_age_sec" in body
```

注意：TestClient `with` 触发 lifespan → config.is_configured()=True → 会调 `gateway.login()`（FakeGateway login 直接 ready）+ 启动调度器线程——与既有 http 测试行为一致，若既有测试有 pattern 处理调度器（如 monkeypatch SubscriptionScheduler.start），沿用之。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_realtime_stale.py -q`
Expected: FAIL（`cache_age_sec` 不存在 / 响应无 stale 逻辑 / health 无新字段）

- [ ] **Step 3: 实现 `app/realtime_service.py`**

`clear_cache` 改为：

```python
    def clear_cache(self) -> None:
        """清空订阅缓存与类型映射（调度器停止订阅时调用）。重置快照时间戳。"""
        with self._lock:
            self._cache.clear()
        self._type_map = {}
        self._last_snapshot_ts = 0.0
```

新增属性（放 `last_snapshot_ts` 方法后）：

```python
    @property
    def cache_age_sec(self) -> float | None:
        """缓存距最近一次快照的秒数。从未收到数据（ts==0）返回 None。"""
        if self._last_snapshot_ts == 0:
            return None
        return time.time() - self._last_snapshot_ts
```

- [ ] **Step 4: 实现 `app/http_app.py` /realtime 路由**

`data = realtime_service.snapshot(code_list, type_set)` 到 `if not data:` 之间的逻辑改为：

```python
        data = realtime_service.snapshot(code_list, type_set)
        stale_fields: dict = {}
        if data:
            age = realtime_service.cache_age_sec
            if age is not None and age > config.stale_max_age_sec:
                # 缓存过旧（超上限）：视为无缓存走 fallback，不无限期返回陈旧数据
                logger.info("request_id=%s 订阅缓存过旧 (%.0fs > %ds)，走 fallback",
                            get_request_id(request), age, config.stale_max_age_sec)
                data = []
            elif age is not None and age > config.stale_threshold_sec:
                # 盘中断线降级：返回 stale 缓存，调用方自行决策
                stale_fields = {"stale": True, "cache_age_sec": int(age)}
        if not data:
            # ... 既有 fallback 逻辑不变 ...
        return {"data": data, **stale_fields}
```

（保留既有 fallback try/except 块原样；仅末尾 return 加 `**stale_fields`。）

- [ ] **Step 5: 实现 `app/health.py`**

`status()` 返回 dict 追加三个键（用 getattr 防御 FakeGateway 缺属性）：

```python
            "auth": auth_state,
            # 登录/重连诊断（SDK 故障排障：一次 /health 看清失败类别与重连次数）
            "last_login_error": getattr(self._gw, "last_login_error", None),
            "reconnect_attempts": getattr(self._gw, "reconnect_attempts", 0),
            "cache_age_sec": (
                self._realtime_svc.cache_age_sec if self._realtime_svc else None
            ),
```

- [ ] **Step 6: `tests/conftest.py` FakeGateway 补属性**

`FakeGateway.__init__` 末尾追加：

```python
        self._last_login_error = None
        self._reconnect_attempts = 0
```

类中追加：

```python
    @property
    def last_login_error(self):
        return self._last_login_error

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts
```

- [ ] **Step 7: `docs/API.md` 补 /realtime stale 说明**

读 `docs/API.md` 的 /realtime 段（约 209-244 行），在响应说明处追加：

```markdown
断线降级字段（仅订阅缓存超过 `STALE_THRESHOLD_SEC`（默认 90s）未更新时附加）：

```json
{"data": [...], "stale": true, "cache_age_sec": 130}
```

- `stale`: true 表示数据为断线期间的最后一次推送缓存，调用方自行决定是否可用。
- `cache_age_sec`: 距最后一次快照推送的秒数。
- 缓存年龄超过 `STALE_MAX_AGE_SEC`（默认 300s）后不再返回 stale 缓存，回退当日历史快照查询，SDK 不可用时报 503。
```

- [ ] **Step 8: 跑测试确认通过**

Run: `python -m pytest tests/test_realtime_stale.py -q`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add app/realtime_service.py app/http_app.py app/health.py tests/conftest.py tests/test_realtime_stale.py docs/API.md
git commit -m "feat(realtime): stale 缓存降级 + /health 登录诊断字段"
```

---

## 集成验收（主会话执行，所有任务完成后）

- [ ] 全量测试一次跑通：`python -m pytest tests/ -q > pytest-full-out.txt 2>&1` 后读结果文件（终端极慢，只跑这一次；失败后修复再跑一次受影响文件）
- [ ] 清理 `pytest-full-out.txt`
- [ ] `docs/API.md`、spec、plan 与实现一致性抽查

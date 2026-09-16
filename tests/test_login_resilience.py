"""登录韧性：SystemExit 兜底 / calendar 保留 / 退避 / last_login_error。"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from app.config import Config
from app.gateway import AmazingDataGateway, GatewayNotReadyError, GatewayQueryError
from app.gateway.base import WEDGE_EXIT_CODE


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
        # 事件门控：test 断言期间阻塞重连线程，消除 in_progress 竞态
        gw._test_release = threading.Event()
        def gated_do_login():
            gw._test_release.wait(timeout=5)
        gw._do_login = gated_do_login
        return gw

    @staticmethod
    def _wait_reconnect_done(gw, timeout=5.0):
        """轮询等待 _reconnect_in_progress 变为 False（重连线程已退出）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not gw._reconnect_in_progress:
                return
            time.sleep(0.02)

    def test_backoff_sequence(self, monkeypatch):
        """连续失败退避 60→120→240→300 封顶。"""
        gw = self._gw(monkeypatch)
        now = time.time()
        # failures=0 → 间隔 60s：61s 前尝试过 → 放行
        gw._reconnect_failures = 0
        gw._last_reconnect_attempt = now - 61
        gw._schedule_reconnect("test")
        assert gw._reconnect_in_progress is True
        # 放行重连线程，等待其退出（gated_do_login 立即返回 → finally 置 in_progress=False）
        gw._test_release.set()
        self._wait_reconnect_done(gw)
        # failures=3 → 间隔 min(60*8, 300)=300s：120s 前尝试 → 拦截
        gw._reconnect_failures = 3
        gw._last_reconnect_attempt = now - 120
        gw._schedule_reconnect("test")
        assert gw._reconnect_in_progress is False
        # 301s 前 → 放行
        gw._last_reconnect_attempt = now - 301
        gw._test_release.clear()
        gw._schedule_reconnect("test")
        assert gw._reconnect_in_progress is True
        gw._test_release.set()
        self._wait_reconnect_done(gw)

    def test_reconnect_attempts_counter(self, monkeypatch):
        gw = self._gw(monkeypatch)
        gw._last_reconnect_attempt = 0.0
        gw._schedule_reconnect("test")
        assert gw.reconnect_attempts == 1
        # 放行重连线程，等待其退出，避免泄漏
        gw._test_release.set()
        self._wait_reconnect_done(gw)

    def test_reconnect_failure_auto_retries_until_success(self, monkeypatch):
        """重连失败后按退避自动重试直到成功，不再一次性停摆。

        2026-09-10 事故：重连因 gateway lock 竞争超时失败一次后，SDK 原生会话
        保活良好不再产生断线事件、查询面 not-ready fail-fast 不再触发超时，
        两个重连触发入口永久停摆（_ready 卡死 4 天，reconnect_attempts 定格 1）。
        """
        monkeypatch.setattr(
            "app.gateway.tgw_events._RECONNECT_COOLDOWN_SEC", 0.02,
        )
        config = Config(username="u", password="p", ip="127.0.0.1", port=12345,
                        auth_required=False, reconnect_max_interval_sec=1)
        gw = AmazingDataGateway(config)
        attempts = [0]

        def flaky_do_login():
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("boom")
            gw._ready = True

        gw._do_login = flaky_do_login
        gw._last_reconnect_attempt = 0.0
        gw._schedule_reconnect("test")
        deadline = time.time() + 5
        while time.time() < deadline and attempts[0] < 3:
            time.sleep(0.02)
        assert attempts[0] == 3  # 失败 2 次后自动重试，第 3 次成功
        assert gw.is_ready() is True
        assert gw._reconnect_failures == 0  # 成功复位
        self._wait_reconnect_done(gw)

    def test_reconnect_skipped_when_already_ready(self, monkeypatch):
        """退避重试触发时会话已被其他路径恢复 → 跳过 login，重试链终止。"""
        monkeypatch.setattr(
            "app.gateway.tgw_events._RECONNECT_COOLDOWN_SEC", 0.02,
        )
        config = Config(username="u", password="p", ip="127.0.0.1", port=12345,
                        auth_required=False, reconnect_max_interval_sec=1)
        gw = AmazingDataGateway(config)
        calls = [0]

        def counting_do_login():
            calls[0] += 1

        gw._do_login = counting_do_login
        gw._ready = True  # 已被调度器自愈 login 等路径恢复
        gw._last_reconnect_attempt = 0.0
        gw._schedule_reconnect("test")
        self._wait_reconnect_done(gw)
        assert calls[0] == 0  # _do 看到 ready 直接返回，未重复 login


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


class TestWedgeExit:
    """楔死主动退出：登录路径超时连续 2 次且本进程曾成功登录 → os._exit(71)。

    2026-09-16 事故实证：原生楔死后 8 轮进程内重连全部失败（get_calendar 挂在
    同一楔死通道 + max_limitation），最终靠 OOM 杀进程才恢复。挂死的 C 层调用
    只有进程死亡才能清除，故连续楔死签名达到阈值即主动退出交由 restart 拉起。
    """

    @staticmethod
    def _make_gw(monkeypatch, exits):
        monkeypatch.setitem(
            sys.modules, "AmazingData", _fake_ad_module(lambda **kwargs: None),
        )
        gw = AmazingDataGateway(_make_config())
        monkeypatch.setattr(
            "app.gateway.session.os._exit", lambda code: exits.append(code),
        )
        return gw

    def _timeout_login(self, gw):
        """模拟登录路径超时：让 _call_sdk_with_timeout 直接抛楔死签名异常。"""
        err = GatewayQueryError(
            "get_calendar 超过 120s 无响应（SDK 线程已隔离为 daemon，会话重建中）"
        )

        def raise_timeout(fn, timeout_sec, label):
            raise err

        gw._call_sdk_with_timeout = raise_timeout
        with pytest.raises(GatewayNotReadyError):
            gw.login()

    def test_double_wedge_timeout_exits(self, monkeypatch):
        """曾成功登录 + 连续 2 次登录路径超时 → 主动 os._exit(WEDGE_EXIT_CODE)。"""
        exits = []
        gw = self._make_gw(monkeypatch, exits)
        gw._ever_ready = True
        self._timeout_login(gw)
        assert exits == []                       # 第 1 次：只计数不退出
        assert gw._wedge_signature_streak == 1
        self._timeout_login(gw)
        assert exits == [WEDGE_EXIT_CODE]        # 第 2 次：主动退出
        assert gw._wedge_signature_streak == 2

    def test_wedge_exit_guarded_before_first_login_success(self, monkeypatch):
        """从未成功登录（启动期故障）→ 只计数不退出，避免 crash-loop。"""
        exits = []
        gw = self._make_gw(monkeypatch, exits)
        for _ in range(3):
            self._timeout_login(gw)
        assert exits == []
        assert gw._wedge_signature_streak == 3

    def test_non_timeout_failures_do_not_exit(self, monkeypatch):
        """SystemExit 路径（max_limitation / SDK 内部 exit(0)）非楔死签名 → 永不退出。"""
        exits = []
        monkeypatch.setitem(
            sys.modules, "AmazingData", _fake_ad_module(lambda **kwargs: exit(0)),
        )
        gw = AmazingDataGateway(_make_config())
        monkeypatch.setattr(
            "app.gateway.session.os._exit", lambda code: exits.append(code),
        )
        gw._ever_ready = True
        for _ in range(4):
            with pytest.raises(GatewayNotReadyError):
                gw.login()
        assert exits == []
        assert gw._wedge_signature_streak == 0

    def test_success_resets_streak_and_marks_ever_ready(self, monkeypatch):
        """成功登录复位楔死计数并置 _ever_ready（此后楔死退出守卫放开）。"""
        exits = []
        gw = self._make_gw(monkeypatch, exits)
        self._timeout_login(gw)
        assert gw._wedge_signature_streak == 1
        # 成功登录：绕过超时包装直调 fn，构造完整 fake SDK 面
        fake = _fake_ad_module(lambda **kwargs: None)

        class _FakeBaseData:
            def get_calendar(self):
                return [20260916]

        fake.BaseData = _FakeBaseData
        fake.MarketData = lambda calendar: None
        fake.InfoData = lambda: None
        monkeypatch.setitem(sys.modules, "AmazingData", fake)
        gw._call_sdk_with_timeout = lambda fn, timeout_sec, label: fn()
        gw.login()
        assert gw.is_ready() is True
        assert gw._ever_ready is True
        assert gw._wedge_signature_streak == 0

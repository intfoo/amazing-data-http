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

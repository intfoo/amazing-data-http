"""session.py 超时包装与 _do_login 自动换锁。"""

import sys
import threading
import types

import pytest

from app.config import Config
from app.gateway import AmazingDataGateway, GatewayNotReadyError, GatewayQueryError


def make_config(timeout=1):
    return Config(username="u", password="p", ip="1.2.3.4", port=3021,
                  sdk_call_timeout_sec=timeout,
                  sdk_session_timeout_sec=timeout)


def _stub_hooks(gw, monkeypatch):
    monkeypatch.setattr(gw, "_install_tgw_event_logger", lambda: None)
    monkeypatch.setattr(gw, "_install_login_spi_probe", lambda: None)
    monkeypatch.setattr(gw, "_schedule_reconnect", lambda reason: None)


def test_do_login_resets_query_lock(monkeypatch):
    """_do_login 开头自动换锁：fake ad 全流程，断言 QueryLock.query_lock 被更换。"""
    leaked = threading.RLock()
    leaked.acquire()
    env_mod = types.ModuleType("AmazingData.environment")

    class QueryLock:
        query_lock = leaked

    env_mod.QueryLock = QueryLock
    ad_mod = types.ModuleType("AmazingData")
    ad_mod.environment = env_mod
    ad_mod.login = lambda **kw: None
    ad_mod.BaseData = lambda: types.SimpleNamespace(get_calendar=lambda: [20240101])
    ad_mod.MarketData = lambda cal: types.SimpleNamespace(calendar=cal)
    ad_mod.InfoData = lambda: types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "AmazingData", ad_mod)
    monkeypatch.setitem(sys.modules, "AmazingData.environment", env_mod)
    gw = AmazingDataGateway(make_config())
    _stub_hooks(gw, monkeypatch)
    gw._do_login()
    assert QueryLock.query_lock is not leaked
    assert gw.is_ready() is True
    leaked.release()


def test_do_login_login_timeout(monkeypatch):
    """ad.login 挂起超过 sdk_session_timeout_sec → GatewayNotReadyError + not ready。"""
    ad_mod = types.ModuleType("AmazingData")
    ad_mod.login = lambda **kw: threading.Event().wait()
    monkeypatch.setitem(sys.modules, "AmazingData", ad_mod)
    gw = AmazingDataGateway(make_config())
    _stub_hooks(gw, monkeypatch)
    with pytest.raises(GatewayNotReadyError):
        gw._do_login()
    assert gw._ready is False


def test_do_login_calendar_timeout(monkeypatch):
    """login 成功但 get_calendar 挂起 → GatewayNotReadyError + not ready。"""
    ad_mod = types.ModuleType("AmazingData")
    ad_mod.login = lambda **kw: None
    ad_mod.logout = lambda **kw: None
    ad_mod.BaseData = lambda: types.SimpleNamespace(
        get_calendar=lambda: threading.Event().wait())
    monkeypatch.setitem(sys.modules, "AmazingData", ad_mod)
    gw = AmazingDataGateway(make_config())
    _stub_hooks(gw, monkeypatch)
    with pytest.raises(GatewayNotReadyError):
        gw._do_login()
    assert gw._ready is False


def test_refresh_calendar_timeout(monkeypatch):
    """refresh_calendar 的 get_calendar 挂起 → GatewayQueryError（消息无连接关键词）。"""
    gw = AmazingDataGateway(make_config())
    monkeypatch.setattr(gw, "_schedule_reconnect", lambda reason: None)
    gw._ready = True
    gw._base_data = types.SimpleNamespace(
        get_calendar=lambda: threading.Event().wait())
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.refresh_calendar()
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_do_login_uses_session_timeout(monkeypatch):
    """登录路径超时读 sdk_session_timeout_sec（与查询路径 sdk_call_timeout_sec 分离）。"""
    seen = []

    def record_call(fn, timeout_sec, label):
        seen.append((label, timeout_sec))
        if label == "login":
            raise GatewayQueryError(
                "login 超过 1s 无响应（SDK 线程已隔离为 daemon，会话重建中）")

    ad_mod = types.ModuleType("AmazingData")
    ad_mod.login = lambda **kw: None
    monkeypatch.setitem(sys.modules, "AmazingData", ad_mod)
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    sdk_call_timeout_sec=600, sdk_session_timeout_sec=1)
    gw = AmazingDataGateway(config)
    _stub_hooks(gw, monkeypatch)
    gw._call_sdk_with_timeout = record_call
    with pytest.raises(GatewayNotReadyError):
        gw._do_login()
    # 超时实参=1（session 值）而非 600（query 值），证明分级生效
    assert seen == [("login", 1)]

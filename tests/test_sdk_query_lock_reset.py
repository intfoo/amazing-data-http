"""_reset_sdk_query_lock 换锁 + _call_sdk_with_timeout BaseException 透传。"""

import sys
import threading
import types

import pytest

from app.config import Config
from app.gateway import AmazingDataGateway, GatewayQueryError


def make_config():
    return Config(username="u", password="p", ip="1.2.3.4", port=3021)


def _fake_sdk_env(monkeypatch):
    """注入 fake AmazingData + AmazingData.environment（query_lock 预 acquire 模拟泄漏）。"""
    leaked = threading.RLock()
    leaked.acquire()
    env_mod = types.ModuleType("AmazingData.environment")

    class QueryLock:
        query_lock = leaked

    env_mod.QueryLock = QueryLock
    ad_mod = types.ModuleType("AmazingData")
    ad_mod.environment = env_mod
    monkeypatch.setitem(sys.modules, "AmazingData", ad_mod)
    monkeypatch.setitem(sys.modules, "AmazingData.environment", env_mod)
    return QueryLock, leaked


def test_reset_sdk_query_lock_replaces_leaked_lock(monkeypatch):
    """换锁：类属性被替换为新 RLock（未锁定），旧锁对象不再被引用。"""
    QueryLock, leaked = _fake_sdk_env(monkeypatch)
    gw = AmazingDataGateway(make_config())
    gw._reset_sdk_query_lock()
    assert QueryLock.query_lock is not leaked
    # Python 3.13 的 _thread.RLock 没有 .locked()，用 acquire(blocking=False) 验证未锁定
    acquired = QueryLock.query_lock.acquire(blocking=False)
    assert acquired, "新锁不应处于锁定状态"
    QueryLock.query_lock.release()
    leaked.release()


def test_reset_sdk_query_lock_silent_without_sdk(monkeypatch):
    """SDK 未导入（ImportError）时静默跳过，不抛异常。"""
    monkeypatch.setitem(sys.modules, "AmazingData", None)  # import 触发 ImportError
    gw = AmazingDataGateway(make_config())
    gw._reset_sdk_query_lock()  # 不抛即通过


def test_call_sdk_with_timeout_passthrough_system_exit():
    """BaseException（如 SDK login 的 SystemExit）必须透传，不得静默吞掉返回 None。"""
    gw = AmazingDataGateway.__new__(AmazingDataGateway)  # 绕过 __init__（同现有超时测试模式）
    gw._ready = True
    gw._schedule_reconnect = lambda reason: None  # 桩掉重连调度

    def _exit():
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        gw._call_sdk_with_timeout(_exit, 1, "probe")

"""query_market.py 超时包装 + get_code_list/get_code_info corruption 检测。"""

import threading
import types

import pytest

from app.config import Config
from app.gateway import AmazingDataGateway, GatewayQueryError


def make_config(timeout=1):
    return Config(username="u", password="p", ip="1.2.3.4", port=3021,
                  sdk_call_timeout_sec=timeout)


def _ready_gw(monkeypatch):
    gw = AmazingDataGateway(make_config())
    monkeypatch.setattr(gw, "_schedule_reconnect", lambda reason: None)
    gw._ready = True
    return gw


def _hang(*_args, **_kwargs):
    threading.Event().wait()


def test_query_kline_timeout(monkeypatch):
    gw = _ready_gw(monkeypatch)
    gw._market_data = types.SimpleNamespace(query_kline=_hang)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.query_kline(["000001.SZ"], None, None, "day")
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_query_snapshot_timeout(monkeypatch):
    gw = _ready_gw(monkeypatch)
    gw._market_data = types.SimpleNamespace(query_snapshot=_hang)
    gw._calendar = [20240101]
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.query_snapshot(["000001.SZ"], trade_date=20240101)
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_get_code_list_timeout(monkeypatch):
    gw = _ready_gw(monkeypatch)
    gw._base_data = types.SimpleNamespace(get_code_list=_hang)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.get_code_list()
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_get_code_info_timeout(monkeypatch):
    gw = _ready_gw(monkeypatch)
    gw._base_data = types.SimpleNamespace(get_code_info=_hang)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.get_code_info()
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_get_code_list_corruption_triggers_rebuild(monkeypatch):
    """get_code_list 抛 'NoneType' → _is_sdk_corruption 命中 → _do_login 重建后抛 GatewayQueryError。"""
    gw = _ready_gw(monkeypatch)

    def _corrupt(security_type="EXTRA_STOCK_A"):
        raise RuntimeError("'NoneType' object is not subscriptable")

    gw._base_data = types.SimpleNamespace(get_code_list=_corrupt)
    calls = []
    monkeypatch.setattr(gw, "_do_login", lambda: calls.append(1))
    with pytest.raises(GatewayQueryError):
        gw.get_code_list()
    assert calls == [1]


def test_get_code_info_corruption_triggers_rebuild(monkeypatch):
    """get_code_info 抛 '查询失败' → corruption 命中 → _do_login 重建。"""
    gw = _ready_gw(monkeypatch)

    def _corrupt(security_type="EXTRA_STOCK_A"):
        raise RuntimeError("查询失败")

    gw._base_data = types.SimpleNamespace(get_code_info=_corrupt)
    calls = []
    monkeypatch.setattr(gw, "_do_login", lambda: calls.append(1))
    with pytest.raises(GatewayQueryError):
        gw.get_code_info()
    assert calls == [1]

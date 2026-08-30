"""query_basedata.py 超时包装（get_fund_share/get_fund_nav/get_adj_factor）。"""

import threading
import types

import pytest

from app.config import Config
from app.gateway import AmazingDataGateway, GatewayQueryError


def make_config(timeout=1):
    return Config(username="u", password="p", ip="1.2.3.4", port=3021,
                  sdk_call_timeout_sec=timeout)


def _hang(*_args, **_kwargs):
    threading.Event().wait()


def _ready_gw(monkeypatch):
    gw = AmazingDataGateway(make_config())
    monkeypatch.setattr(gw, "_schedule_reconnect", lambda reason: None)
    gw._ready = True
    return gw


def test_get_fund_share_timeout(monkeypatch):
    gw = _ready_gw(monkeypatch)
    gw._info_data = types.SimpleNamespace(get_fund_share=_hang)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.get_fund_share(["510300.SH"])
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_get_fund_nav_timeout(monkeypatch):
    gw = _ready_gw(monkeypatch)
    gw._info_data = types.SimpleNamespace(get_fund_nav=_hang)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.get_fund_nav(["510300.SH"])
    assert "无响应" in str(exc_info.value)
    assert gw._ready is False


def test_get_adj_factor_uses_config_timeout(monkeypatch):
    """get_adj_factor 超时值读 Config.sdk_call_timeout_sec（1s 即超时证明生效）。"""
    gw = _ready_gw(monkeypatch)
    gw._base_data = types.SimpleNamespace(get_adj_factor=_hang)
    with pytest.raises(GatewayQueryError) as exc_info:
        gw.get_adj_factor(["510300.SH"])
    assert "无响应" in str(exc_info.value)

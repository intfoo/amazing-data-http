import pytest
from app.config import Config
from app.gateway import GatewayNotReadyError, GatewayQueryError


def make_config():
    return Config(username="u", password="p", ip="1.2.3.4", port=3021)


def test_gateway_not_ready_before_login():
    from app.gateway import AmazingDataGateway
    gw = AmazingDataGateway(make_config())
    assert gw.is_ready() is False
    with pytest.raises(GatewayNotReadyError):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")


def test_gateway_query_unsupported_period():
    from app.gateway import AmazingDataGateway
    gw = AmazingDataGateway(make_config())
    gw._ready = True
    gw._market_data = object()
    with pytest.raises(GatewayQueryError, match="unsupported period"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "invalid_period")


def test_is_connection_error_detects_keywords():
    """_is_connection_error 应识别常见连接类错误关键词。"""
    from app.gateway import _is_connection_error
    assert _is_connection_error(RuntimeError("Connection reset by peer"))
    assert _is_connection_error(RuntimeError("Operation timed out"))
    assert _is_connection_error(RuntimeError("broken pipe"))
    assert _is_connection_error(RuntimeError("server closed connection"))
    assert not _is_connection_error(ValueError("unsupported period"))
    assert not _is_connection_error(RuntimeError("invalid code"))


def test_query_kline_reconnects_on_connection_error(monkeypatch):
    """query_kline 遇连接类错误时应 relogin + 重试一次，重试成功则返回结果。"""
    import pandas as pd
    from app.gateway import AmazingDataGateway
    gw = AmazingDataGateway(make_config())
    gw._ready = True
    calls = {"login": 0, "query": 0}

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            calls["query"] += 1
            if calls["query"] == 1:
                raise RuntimeError("Connection reset by peer")
            return {"000001.SZ": pd.DataFrame({"code": ["000001.SZ"], "close": [10.3]})}

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(gw, "_do_login", lambda: calls.__setitem__("login", calls["login"] + 1))
    result = gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")
    assert calls["login"] == 1
    assert calls["query"] == 2
    assert "000001.SZ" in result


def test_query_kline_raises_after_reconnect_failure(monkeypatch):
    """relogin 后重试仍失败时应抛 GatewayQueryError。"""
    from app.gateway import AmazingDataGateway, GatewayQueryError
    gw = AmazingDataGateway(make_config())
    gw._ready = True

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            raise RuntimeError("Connection refused")

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(gw, "_do_login", lambda: None)
    with pytest.raises(GatewayQueryError, match="after reconnect"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")


def test_query_kline_non_connection_error_does_not_relogin(monkeypatch):
    """非连接类错误不应触发 relogin。"""
    from app.gateway import AmazingDataGateway, GatewayQueryError
    gw = AmazingDataGateway(make_config())
    gw._ready = True
    login_called = [0]

    class FakeMarketData:
        def query_kline(self, codes, **kwargs):
            raise ValueError("invalid code format")

    gw._market_data = FakeMarketData()
    monkeypatch.setattr(gw, "_do_login", lambda: login_called.__setitem__(0, login_called[0] + 1))
    with pytest.raises(GatewayQueryError, match="query failed"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")
    assert login_called[0] == 0


def test_is_connection_error_eof_occurred_matches():
    """'EOF occurred in violation of protocol' 应匹配为连接错误。"""
    from app.gateway import _is_connection_error
    assert _is_connection_error(RuntimeError("EOF occurred in violation of protocol"))


def test_is_connection_error_bare_eof_field_name_does_not_match():
    """仅含 'eof' 子串但非连接错误（如字段名 'some_eof_field'）不应误匹配。"""
    from app.gateway import _is_connection_error
    assert not _is_connection_error(ValueError("invalid some_eof_field value"))


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

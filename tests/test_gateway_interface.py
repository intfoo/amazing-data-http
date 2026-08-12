from app.gateway import Gateway, PERIOD_MAP, GatewayError, GatewayNotReadyError, GatewayQueryError
from tests.conftest import FakeGateway


def test_fake_gateway_satisfies_protocol():
    gw = FakeGateway()
    assert isinstance(gw, Gateway) or hasattr(gw, "login")


def test_period_map_contains_day():
    assert PERIOD_MAP["day"] == "day"


def test_period_map_contains_all_periods():
    expected = {"day", "min1", "min3", "min5", "min10", "min15",
                "min30", "min60", "min120", "week", "month", "season", "year"}
    assert expected <= set(PERIOD_MAP.keys())


def test_gateway_error_hierarchy():
    assert issubclass(GatewayNotReadyError, GatewayError)
    assert issubclass(GatewayQueryError, GatewayError)


def test_fake_gateway_get_code_list():
    from tests.conftest import FakeGateway
    gw = FakeGateway(ready=True)
    codes = gw.get_code_list()
    assert isinstance(codes, list)
    assert len(codes) > 0


def test_fake_gateway_implements_get_adj_factor():
    """FakeGateway 扩展 get_adj_factor 后仍满足 Gateway Protocol（@runtime_checkable）。"""
    from tests.conftest import FakeGateway, make_adj_factor_df
    from app.gateway import Gateway
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    assert isinstance(gw, Gateway)  # Protocol runtime check
    df = gw.get_adj_factor(["000001.SZ"])
    assert df is not None
    assert not df.empty


def test_fake_gateway_get_adj_factor_not_ready():
    from tests.conftest import FakeGateway
    from app.gateway import GatewayNotReadyError
    import pytest
    gw = FakeGateway(ready=False)
    with pytest.raises(GatewayNotReadyError):
        gw.get_adj_factor(["000001.SZ"])


def test_fake_gateway_subscription_noop():
    from tests.conftest import FakeGateway
    gw = FakeGateway(ready=True)
    gw.start_snapshot_subscription(["000001.SZ"], on_data=lambda d: None)
    assert gw.sub_start_called == 1
    gw.stop_subscription()
    assert gw.sub_stop_called == 1


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


def test_fake_gateway_has_calendar_set_property():
    """FakeGateway 暴露 calendar_set（Protocol @runtime_checkable 需要）。"""
    gw = FakeGateway(ready=True)
    assert gw.calendar_set == frozenset()
    gw2 = FakeGateway(ready=True, calendar=[20240102, 20240103])
    assert gw2.calendar_set == frozenset({20240102, 20240103})
    assert isinstance(gw2, Gateway)


def test_stop_subscription_acquires_sdk_lock():
    """预持 gateway._lock 时 stop_subscription 必须阻塞等锁（证明经过 _sdk_lock）。"""
    import threading
    from app.config import Config
    from app.gateway import AmazingDataGateway

    gw = AmazingDataGateway(Config(username="u", password="p", ip="1.2.3.4", port=1))
    entered = threading.Event()   # stop_subscription 已返回
    gw._lock.acquire()
    try:
        t = threading.Thread(
            target=lambda: (gw.stop_subscription(), entered.set()), daemon=True
        )
        t.start()
        t.join(timeout=1.0)
        assert not entered.is_set()  # 仍在等 _sdk_lock（未持有锁时会立即返回）
    finally:
        gw._lock.release()
    t.join(timeout=5)
    assert entered.is_set()  # 释放锁后正常返回

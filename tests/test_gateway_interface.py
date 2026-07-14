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


def test_fake_gateway_subscription_noop():
    from tests.conftest import FakeGateway
    gw = FakeGateway(ready=True)
    gw.start_snapshot_subscription(["000001.SZ"], on_data=lambda d: None)
    assert gw.sub_start_called == 1
    gw.stop_subscription()
    assert gw.sub_stop_called == 1

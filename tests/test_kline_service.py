import numpy as np
import pandas as pd
import pytest

from app.gateway import GatewayNotReadyError, GatewayQueryError
from app.kline_service import KlineService, to_sdk_date
from tests.conftest import FakeGateway, make_daily_df


def test_to_sdk_date():
    assert to_sdk_date("2024-01-01") == 20240101
    assert to_sdk_date("2024-12-31") == 20241231


def test_to_sdk_date_invalid():
    with pytest.raises(ValueError):
        to_sdk_date("20240101")
    with pytest.raises(ValueError):
        to_sdk_date("not-a-date")


def test_query_returns_flattened_records():
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert len(data) == 1
    row = data[0]
    assert row["code"] == "000001.SZ"
    assert row["open"] == 10.2
    assert row["close"] == 10.3
    assert row["volume"] == 1234567


def test_query_passes_sdk_dates_and_day_period():
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20240101
    assert call["end_date"] == 20240131
    assert call["period"] == "day"


def test_query_empty_result():
    gw = FakeGateway(ready=True, result={})
    svc = KlineService(gw)
    assert svc.query(["000001.SZ"], "2024-01-01", "2024-01-31") == []


def test_query_empty_dataframe():
    df = pd.DataFrame({"code": [], "open": []})
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    assert svc.query(["000001.SZ"], "2024-01-01", "2024-01-31") == []


def test_query_multiple_codes():
    result = {
        "000001.SZ": make_daily_df("000001.SZ"),
        "600000.SH": make_daily_df("600000.SH"),
    }
    gw = FakeGateway(ready=True, result=result)
    svc = KlineService(gw)
    data = svc.query(["000001.SZ", "600000.SH"], "2024-01-02", "2024-01-02")
    assert len(data) == 2
    codes = {row["code"] for row in data}
    assert codes == {"000001.SZ", "600000.SH"}


def test_query_dataframe_without_code_column():
    df = pd.DataFrame(
        {"open": [10.2], "close": [10.3]},
        index=pd.Index(["2024-01-02"], name="trade_time"),
    )
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert data[0]["code"] == "000001.SZ"
    assert data[0]["open"] == 10.2


def test_query_gateway_not_ready():
    gw = FakeGateway(ready=False)
    svc = KlineService(gw)
    with pytest.raises(GatewayNotReadyError):
        svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")


def test_query_gateway_error():
    gw = FakeGateway(ready=True, result=None)
    svc = KlineService(gw)
    with pytest.raises(GatewayQueryError):
        svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")

import numpy as np
import pandas as pd
import pytest

from app.gateway import GatewayNotReadyError, GatewayQueryError
from app.kline_service import KlineService, to_sdk_date
from tests.conftest import FakeGateway, make_daily_df


def test_to_sdk_date():
    assert to_sdk_date("2024-01-01") == 20240101
    assert to_sdk_date("2024-12-31") == 20241231


def test_to_sdk_date_iso_datetime():
    """ISO datetime 格式应截断时间部分，只取日期。"""
    assert to_sdk_date("2025-07-14T00:00:00") == 20250714
    assert to_sdk_date("2025-07-14T23:59:59") == 20250714
    assert to_sdk_date("2025-07-14T00:00:00.123") == 20250714
    assert to_sdk_date("2025-07-14T00:00:00+08:00") == 20250714


def test_to_sdk_date_compact_formats():
    """Python 3.11+ fromisoformat 额外接受的紧凑格式。"""
    assert to_sdk_date("20240101") == 20240101  # 8 位紧凑日期
    assert to_sdk_date("20240101T000000") == 20240101  # 紧凑 datetime


def test_to_sdk_date_invalid():
    with pytest.raises(ValueError):
        to_sdk_date("not-a-date")
    with pytest.raises(ValueError):
        to_sdk_date("2025/07/14")  # 斜杠分隔非 ISO
    with pytest.raises(ValueError):
        to_sdk_date("2025-13-01")  # 非法月份


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


def test_query_passes_iso_datetime_truncated():
    """ISO datetime 应被截断为日期后传给 gateway。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2025-07-14T00:00:00", "2025-07-14T23:59:59")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20250714
    assert call["end_date"] == 20250714


def test_query_optional_dates_pass_none_to_gateway():
    """未传日期时应透传 None 给 gateway，由 gateway 决定是否传 SDK。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"])
    call = gw.query_calls[0]
    assert call["begin_date"] is None
    assert call["end_date"] is None
    assert call["period"] == "day"


def test_query_optional_one_side_only():
    """只传一端时，另一端应为 None。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], start_time="2024-01-01")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20240101
    assert call["end_date"] is None

    svc.query(["000001.SZ"], end_time="2024-12-31")
    call = gw.query_calls[1]
    assert call["begin_date"] is None
    assert call["end_date"] == 20241231


def test_query_reversed_dates_raises():
    """start > end 应抛 ValueError（HTTP 层转 422）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    with pytest.raises(ValueError, match="start_time must not be later than end_time"):
        svc.query(["000001.SZ"], "2024-01-31", "2024-01-01")


def test_query_same_day_iso_datetime_not_reversed():
    """同一天不同时间精度不应触发 reversed 校验（按日期比较，非字符串比较）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    # start 带时间，end 纯日期，同一天，不应报错
    svc.query(["000001.SZ"], "2024-01-01T23:59:59", "2024-01-01")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20240101
    assert call["end_date"] == 20240101


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


def test_query_minute_passes_period():
    """分钟K应把 period 透传给 gateway。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-01", "2024-01-31", period="min5")
    call = gw.query_calls[0]
    assert call["period"] == "min5"


def test_query_minute_default_period_is_day():
    """不传 period 时默认 'day'，保持 /daily 向后兼容。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")
    assert gw.query_calls[0]["period"] == "day"

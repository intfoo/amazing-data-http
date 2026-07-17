from datetime import datetime, timedelta

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


def test_query_day_truncates_kline_time_to_date():
    """日 K 的 kline_time 应截断为 yyyy-MM-dd（不含时分秒）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert len(data) == 1
    # make_daily_df 的 kline_time 是 Timestamp "2024-01-02T00:00:00"
    # serializer 转为 "2024-01-02T00:00:00"，日 K 截断后应为 "2024-01-02"
    assert data[0]["kline_time"] == "2024-01-02"


def test_query_minute_keeps_kline_time_full_datetime():
    """分钟K的 kline_time 保留完整 datetime（不截断）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02", period="min5")
    assert len(data) == 1
    # 分钟周期不截断，保留 serializer 输出的 ISO 字符串
    assert data[0]["kline_time"] == "2024-01-02T00:00:00"


def test_query_day_truncates_kline_time_idempotent_for_date_only():
    """kline_time 已是 yyyy-MM-dd 格式时截断幂等（不破坏）。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.Timestamp("2024-01-02")],
        "open": [10.2],
        "close": [10.3],
    })
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert data[0]["kline_time"] == "2024-01-02"


def test_query_day_kline_time_none_preserved():
    """kline_time 为 None（NaT）时保持 None，不抛异常。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.NaT],
        "open": [10.2],
        "close": [10.3],
    })
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert data[0]["kline_time"] is None


def test_query_minute_adds_kline_time_utc():
    """分钟K应附加 kline_time_utc（视为 UTC+8 转 UTC，带 Z 后缀）。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.Timestamp("2024-01-02T09:30:00")],
        "open": [10.2],
        "close": [10.3],
    })
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02", period="min5")
    row = data[0]
    # kline_time 保留原 ISO 字符串；kline_time_utc 为 UTC（09:30 UTC+8 → 01:30）
    assert row["kline_time"] == "2024-01-02T09:30:00"
    assert row["kline_time_utc"] == "2024-01-02T01:30:00"
    # kline_time_utc 应紧跟 kline_time 之后
    keys = list(row.keys())
    assert keys.index("kline_time_utc") == keys.index("kline_time") + 1


def test_query_day_has_no_kline_time_utc():
    """日 K 不应附加 kline_time_utc 字段。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert "kline_time_utc" not in data[0]


def test_query_minute_kline_time_nat_utc_is_none():
    """分钟K的 kline_time 为 NaT 时，kline_time_utc 应为 None。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.NaT],
        "open": [10.2],
        "close": [10.3],
    })
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02", period="min5")
    assert data[0]["kline_time"] is None
    assert data[0]["kline_time_utc"] is None


def test_query_minute_midnight_crosses_day_boundary():
    """kline_time 00:00 UTC+8 应转为前一天 16:00Z（跨日边界）。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.Timestamp("2024-01-02T00:00:00")],
        "open": [10.2],
        "close": [10.3],
    })
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02", period="min1")
    assert data[0]["kline_time_utc"] == "2024-01-01T16:00:00"


def test_query_minute_default_range_last_year():
    """minute 不传日期时默认 begin_date 为近一年，end_date 为 None（取到最新）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], period="min1")  # 不传 start_time/end_time
    call = gw.query_calls[0]
    assert call["begin_date"] is not None
    expected = int((datetime.now() - timedelta(days=365)).strftime("%Y%m%d"))
    assert abs(call["begin_date"] - expected) <= 1  # 允许 1 天误差
    assert call["end_date"] is None


def test_query_day_default_uses_sdk_default():
    """day 不传日期时 begin_date/end_date 为 None（SDK 默认 20240101~20991231）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"])  # period 默认 day
    call = gw.query_calls[0]
    assert call["begin_date"] is None
    assert call["end_date"] is None


def test_query_minute_with_dates_overrides_default():
    """minute 传了日期时用用户日期，不应用默认近一年。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-06-01", "2024-06-30", period="min5")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20240601
    assert call["end_date"] == 20240630


def test_query_minute_only_start_still_applies_default_end():
    """minute 只传 start_time 时 begin 用用户值，end_date 为 None。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], start_time="2025-01-01", period="min5")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20250101
    assert call["end_date"] is None


def test_flatten_does_not_mutate_gateway_result():
    """_flatten 改 kline_time 列后，gateway 返回的原始 DataFrame 不应被污染。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "kline_time": [pd.Timestamp("2024-01-02")],
        "open": [10.2], "high": [10.4], "low": [10.1],
        "close": [10.3], "volume": [100], "amount": [1000.0],
    })
    original_time = df["kline_time"].iloc[0]
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    # 原始 df 的 kline_time 仍是 Timestamp，未被 strftime 改成字符串
    assert df["kline_time"].iloc[0] == original_time
    assert isinstance(df["kline_time"].iloc[0], pd.Timestamp)


def test_query_minute_default_range_uses_shanghai_timezone(monkeypatch):
    """minute 默认 begin_date 应基于 UTC+8 计算，不受运行环境 TZ 影响。"""
    from app.kline_service import _SHANGHAI_TZ
    from datetime import datetime, timezone, timedelta
    # 固定 UTC 2024-07-16 16:00 = 北京时间 2024-07-17 00:00
    fake_now = datetime(2024, 7, 16, 16, 0, 0, tzinfo=timezone.utc).astimezone(_SHANGHAI_TZ)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now if tz else fake_now.replace(tzinfo=None)

    monkeypatch.setattr("app.kline_service.datetime", FakeDateTime)
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], period="min1")
    call = gw.query_calls[0]
    expected = int((fake_now - timedelta(days=365)).strftime("%Y%m%d"))
    assert call["begin_date"] == expected
    assert call["end_date"] is None

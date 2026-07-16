import math
import numpy as np
import pandas as pd
from datetime import datetime, date
from app.serializer import serialize_value, serialize_dataframe


def test_serialize_numpy_int():
    assert serialize_value(np.int64(42)) == 42
    assert isinstance(serialize_value(np.int64(42)), int)


def test_serialize_numpy_float():
    assert serialize_value(np.float64(3.14)) == 3.14
    assert isinstance(serialize_value(np.float64(3.14)), float)


def test_serialize_numpy_float_nan():
    assert serialize_value(np.float64(float("nan"))) is None


def test_serialize_numpy_bool():
    assert serialize_value(np.bool_(True)) is True
    assert serialize_value(np.bool_(False)) is False


def test_serialize_datetime():
    dt = datetime(2024, 1, 2, 15, 30, 0)
    assert serialize_value(dt) == "2024-01-02T15:30:00"


def test_serialize_date():
    assert serialize_value(date(2024, 1, 2)) == "2024-01-02"


def test_serialize_pandas_timestamp():
    ts = pd.Timestamp("2024-01-02")
    assert serialize_value(ts) == "2024-01-02T00:00:00"


def test_serialize_nat():
    assert serialize_value(pd.NaT) is None


def test_serialize_python_nan():
    assert serialize_value(float("nan")) is None


def test_serialize_none():
    assert serialize_value(None) is None


def test_serialize_plain_values_passthrough():
    assert serialize_value(42) == 42
    assert serialize_value("hello") == "hello"
    assert serialize_value(3.14) == 3.14


def test_serialize_dataframe_basic():
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "open": [10.2],
        "volume": [np.int64(1234567)],
    })
    result = serialize_dataframe(df)
    assert result == [{"code": "000001.SZ", "open": 10.2, "volume": 1234567}]


def test_serialize_dataframe_with_nan():
    df = pd.DataFrame({
        "open": [10.2, float("nan")],
        "close": [np.float64(float("nan")), 10.5],
    })
    result = serialize_dataframe(df)
    assert result == [{"open": 10.2, "close": None}, {"open": None, "close": 10.5}]


def test_serialize_dataframe_resets_index():
    df = pd.DataFrame({"open": [10.2]}, index=pd.Index(["2024-01-02"], name="trade_time"))
    result = serialize_dataframe(df)
    assert result == [{"trade_time": "2024-01-02", "open": 10.2}]


def test_serialize_dataframe_empty():
    df = pd.DataFrame({"open": [], "close": []})
    assert serialize_dataframe(df) == []


def test_serialize_dataframe_with_datetime_column():
    df = pd.DataFrame({
        "trade_time": [pd.Timestamp("2024-01-02")],
        "close": [10.3],
    })
    result = serialize_dataframe(df)
    assert result == [{"trade_time": "2024-01-02T00:00:00", "close": 10.3}]


def test_serialize_dataframe_object_column_with_timestamp():
    """object dtype 列中残留 Timestamp 应被 serialize_value 兜底为 ISO 字符串。"""
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "mixed": [pd.Timestamp("2024-01-02T09:30:00")],
    })
    result = serialize_dataframe(df)
    assert result == [{"code": "000001.SZ", "mixed": "2024-01-02T09:30:00"}]


def test_serialize_dataframe_large_volume_correctness():
    """大数据量向量化后行为与逐条一致：numpy 标量转原生、NaN 转 None、datetime 转 ISO。"""
    n = 5000
    df = pd.DataFrame({
        "code": ["000001.SZ"] * n,
        "kline_time": pd.date_range("2024-01-02", periods=n, freq="D"),
        "open": [10.2] * n,
        "close": [float("nan")] * n,
        "volume": np.int64(1234567) * np.ones(n, dtype=np.int64),
    })
    result = serialize_dataframe(df)
    assert len(result) == n
    assert isinstance(result[0]["volume"], int)
    assert result[0]["volume"] == 1234567
    assert result[0]["close"] is None
    assert result[0]["kline_time"] == "2024-01-02T00:00:00"
    assert result[-1]["kline_time"].startswith("20")

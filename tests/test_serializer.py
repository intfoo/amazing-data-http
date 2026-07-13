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

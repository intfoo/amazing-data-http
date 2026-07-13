"""DataFrame / NumPy / datetime → JSON 安全值序列化器。

SDK 返回的 pandas DataFrame 含 NumPy 标量和 datetime 索引，
这些类型无法直接 json.dumps。本模块负责将它们转为 Python 原生类型，
NaN/NaT 转为 None（JSON null），不重命名字段。
"""

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def serialize_value(v: Any) -> Any:
    """将单个标量值转为 JSON 安全类型。

    处理顺序很重要：
    - pd.NaT 是 datetime 子类，必须在 datetime 检查之前拦截，否则会返回 "NaT" 字符串
    - NumPy 标量先于 Python 原生类型检查（np.int64 不是 Python int）
    - 末尾的 pd.isna 兜底处理未显式列出的缺失值类型
    """
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    # NumPy 标量 → Python 原生
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        val = float(v)
        return None if math.isnan(val) else val
    if isinstance(v, (np.bool_,)):
        return bool(v)
    # pd.NaT 必须在 datetime/date 之前检查（NaT 是 datetime 子类）
    if v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.isoformat()
    if isinstance(v, (datetime, date)):
        if pd.isna(v):
            return None
        return v.isoformat()
    # 兜底：用 pandas 检测其他缺失值（如 None 嵌在 object 列中）
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def serialize_dataframe(df: pd.DataFrame) -> list[dict]:
    """将 DataFrame 转为 JSON 安全的 list[dict]。

    若 DataFrame 有命名索引（如 trade_time），先 reset_index 将索引变为普通列，
    使日期数据不出现在 JSON 之外。无名索引（RangeIndex）不需要 reset。
    """
    if df is None or df.empty:
        return []
    df_to_use = df.reset_index() if df.index.name is not None else df
    records = df_to_use.to_dict(orient="records")
    return [{k: serialize_value(v) for k, v in record.items()} for record in records]

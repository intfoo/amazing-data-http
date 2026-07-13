import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def serialize_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        val = float(v)
        return None if math.isnan(val) else val
    if isinstance(v, (np.bool_,)):
        return bool(v)
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
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def serialize_dataframe(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    df_to_use = df.reset_index() if df.index.name is not None else df
    records = df_to_use.to_dict(orient="records")
    return [{k: serialize_value(v) for k, v in record.items()} for record in records]

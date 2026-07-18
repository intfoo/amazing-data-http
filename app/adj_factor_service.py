"""AdjFactorService：HTTP 日期参数 → SDK get_adj_factor → 宽表 melt → 日期过滤 → 序列化。

职责边界：
- 调用 gateway.get_adj_factor 获取 SDK 宽表 DataFrame（index=交易日期, columns=股票代码）
- melt 成长表 [{code, trade_date, adj_factor}]，dropna 过滤非除权日
- 按 start_time/end_time 过滤 trade_date（SDK get_adj_factor 不支持日期参数，服务端过滤）
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：字段重命名、单位换算、复权计算（由主项目 YAML field_map 完成）

性能：melt/dropna/过滤在 DataFrame 层向量化完成，避免逐条 dict 操作。
"""

import logging
import time
from datetime import datetime

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe

logger = logging.getLogger("amazingdata.adj_factor")


def _parse_iso(iso: str) -> datetime:
    """ISO 日期/日期时间字符串 → datetime。解析失败抛 ValueError（→ HTTP 422）。

    与 kline_service.to_sdk_date 共享 fromisoformat 解析，错误信息格式一致。
    """
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"date must be YYYY-MM-DD or ISO datetime (e.g. 2024-01-01 / 2024-01-01T00:00:00), got: {iso}"
        )


class AdjFactorService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(
        self,
        codes: list[str],
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """查询除权因子，返回展平后的记录列表 [{code, trade_date, adj_factor}]。

        start_time/end_time 可选；为 None 时该侧不过滤。
        SDK get_adj_factor 不支持日期参数，此处全量拉取后按 trade_date 过滤。
        空结果返回 []（HTTP 层包装为 {"data": []}）。
        """
        start_dt = _parse_iso(start_time) if start_time else None
        end_dt = _parse_iso(end_time) if end_time else None
        if start_dt is not None and end_dt is not None and start_dt > end_dt:
            raise ValueError("start_time must not be later than end_time")

        t0 = time.monotonic()
        df = self._gw.get_adj_factor(codes)
        t1 = time.monotonic()
        records = self._process(df, start_dt, end_dt)
        t2 = time.monotonic()
        logger.info(
            "adj_factor query: gateway=%.3fs process=%.3fs codes=%d records=%d",
            t1 - t0, t2 - t1, len(codes), len(records),
        )
        return records

    @staticmethod
    def _process(df: pd.DataFrame, start_dt, end_dt) -> list[dict]:
        """SDK DataFrame → melt → 日期过滤 → 序列化。"""
        if df is None or df.empty:
            return []
        long_df = AdjFactorService._melt_and_normalize(df)
        long_df = AdjFactorService._filter_by_date(long_df, start_dt, end_dt)
        if long_df.empty:
            return []
        return serialize_dataframe(long_df)

    @staticmethod
    def _melt_and_normalize(df: pd.DataFrame) -> pd.DataFrame:
        """SDK 宽表/长表 → code/trade_date/adj_factor 长表。dropna 过滤非除权日。

        判定顺序：先看是否已是长表（含 code + adj_factor 列），否则按宽表处理。
        """
        if "code" in df.columns and "adj_factor" in df.columns:
            # 长表形态：规范化日期列名
            rename: dict[str, str] = {}
            for src in ("timestamp", "date", "trade_date"):
                if src in df.columns and "trade_date" not in df.columns:
                    rename[src] = "trade_date"
                    break
            if rename:
                df = df.rename(columns=rename)
            df = df.dropna(subset=["adj_factor"])
        else:
            # 宽表形态：index=交易日期, columns=股票代码
            df = df.reset_index()
            date_col = df.columns[0]  # 按位置取日期列（index 可能无名）
            df = df.melt(id_vars=[date_col], var_name="code", value_name="adj_factor")
            df = df.rename(columns={date_col: "trade_date"})
            df = df.dropna(subset=["adj_factor"])
        df = AdjFactorService._normalize_trade_date_str(df)
        return df[["code", "trade_date", "adj_factor"]]

    @staticmethod
    def _normalize_trade_date_str(df: pd.DataFrame) -> pd.DataFrame:
        """trade_date 列统一为 YYYY-MM-DD 字符串。

        datetime64 → dt.strftime；其他类型 → to_datetime 解析后 strftime。
        同 _truncate_kline_time_in_df（kline_service.py:131-141）模式：serialize_dataframe
        会把 datetime 转 ISO datetime，需在此显式截断为日期。
        """
        col = df["trade_date"]
        if pd.api.types.is_datetime64_any_dtype(col):
            df["trade_date"] = col.dt.strftime("%Y-%m-%d")
        else:
            try:
                df["trade_date"] = pd.to_datetime(col, errors="coerce").dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
        return df

    @staticmethod
    def _filter_by_date(df: pd.DataFrame, start_dt, end_dt) -> pd.DataFrame:
        """按 trade_date（YYYY-MM-DD 字符串）过滤。start/end 为 None 时该侧不过滤。"""
        if df.empty:
            return df
        mask = pd.Series([True] * len(df), index=df.index)
        if start_dt is not None:
            mask &= df["trade_date"] >= start_dt.strftime("%Y-%m-%d")
        if end_dt is not None:
            mask &= df["trade_date"] <= end_dt.strftime("%Y-%m-%d")
        return df[mask]

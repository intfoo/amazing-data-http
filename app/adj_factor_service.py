"""AdjFactorService：HTTP 日期参数 → SDK get_adj_factor → 宽表过滤 → 序列化。

职责边界：
- 调用 gateway.get_adj_factor 获取 SDK 宽表 DataFrame（index=交易日期, columns=股票代码）
- 宽表层面过滤列(codes)+日期(index)+非1.0值(stack)，避免 melt 全表导致内存峰值爆炸
- 按 start_time/end_time 过滤 trade_date（SDK get_adj_factor 不支持日期参数，服务端过滤）
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：字段重命名、单位换算、复权计算（由主项目 YAML field_map 完成）

性能：过滤在 DataFrame 层向量化完成（where+stack），避免 melt 全表（43M 行 ~1GB）。
内存：先过滤列（全市场350MB→请求codes14MB），再过滤日期行，最后 stack 提取事件行（几百行）。
"""

from __future__ import annotations

import gc
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        try:
            records = self._process(df, start_dt, end_dt, codes=codes)
        finally:
            # SDK 返回的宽表可能很大（is_local=True 且本地无缓存时，SDK 远程拉取
            # 全市场写本地，返回全市场宽表 5000+ 股 × 8687 交易日 ≈ 350MB）。
            # 显式 del + gc.collect 防止多次调用累积导致 OOM。
            del df
            gc.collect()
        t2 = time.monotonic()
        logger.info(
            "adj_factor query: gateway=%.3fs process=%.3fs codes=%d records=%d",
            t1 - t0, t2 - t1, len(codes), len(records),
        )
        return records

    @staticmethod
    def _process(
        df: pd.DataFrame, start_dt, end_dt, codes: list[str] | None = None,
    ) -> list[dict]:
        """SDK DataFrame → 过滤 → 序列化。

        宽表路径优化：先在宽表层面过滤列(codes)+日期(index)+非1.0值(stack)，
        避免 melt 全表（全市场 43M 行）导致内存峰值爆炸（~1.7GB → ~30MB）。
        """
        if df is None or df.empty:
            return []
        is_wide = not ("code" in df.columns and "adj_factor" in df.columns)
        if is_wide:
            long_df = AdjFactorService._wide_to_event_rows(df, start_dt, end_dt, codes)
        else:
            long_df = AdjFactorService._melt_and_normalize(df)
            long_df = AdjFactorService._filter_by_date(long_df, start_dt, end_dt)
        if long_df.empty:
            return []
        return serialize_dataframe(long_df)

    @staticmethod
    def _wide_to_event_rows(
        df: pd.DataFrame, start_dt, end_dt, codes: list[str] | None = None,
    ) -> pd.DataFrame:
        """宽表 → 事件行长表。先过滤列/日期，再 stack 提取非 1.0 值。

        内存优化核心：避免 melt 全表（全市场 5000 股 × 8687 日 = 43M 行长表 ~1GB）。
        改为：1) 过滤列只保留请求的 codes（350MB→14MB）；2) 过滤日期行；
        3) where(df!=1.0).stack() 只提取非 1.0 事件行（结果几百行）。
        峰值从 ~1.7GB 降到 ~30MB。

        stack 逻辑：where(≠1.0) 把 1.0→NaN（非除权日），NaN 保持 NaN（稀疏表兜底），
        stack().dropna() 丢弃所有 NaN，结果只含真除权事件（adj_factor≠1.0 且非 NaN）。
        """
        import pandas as pd  # 延迟加载：空闲时不占内存
        # 1. 过滤列：SDK is_local=True 且本地无缓存时返回全市场宽表，只保留请求的 codes
        if codes is not None and len(codes) > 0:
            code_set = {str(c) for c in codes}
            cols_to_keep = [c for c in df.columns if str(c) in code_set]
            if cols_to_keep:
                df = df[cols_to_keep]
            else:
                # 宽表不含任何请求的 codes = SDK 返回数据与请求不符（实测场景：
                # 本地缓存部分重建，is_local 增量合并失效）。兜底"使用全量宽表"会把
                # 不相关 code 的因子行返回给调用方（静默错数据，2026-08-28 线上复核
                # 发现），改为报错日志 + 返回空结果。
                logger.error(
                    "列过滤未匹配任何 codes，返回空结果（SDK 宽表缺请求的 codes，"
                    "疑似本地缓存部分重建）: codes_sample=%s columns_sample=%s",
                    codes[:3], list(df.columns[:3]),
                )
                return pd.DataFrame(columns=["code", "trade_date", "adj_factor"])

        # 2. 过滤日期行（index = 交易日期），在宽表层面过滤比 melt 后省内存
        df = AdjFactorService._filter_wide_by_date(df, start_dt, end_dt)

        if df.empty:
            return pd.DataFrame(columns=["code", "trade_date", "adj_factor"])

        # 3. 将 index 转为 YYYY-MM-DD 字符串，避免 stack 后 trade_date 列类型问题
        #    （int index 如 20240101 经 pd.to_datetime 会被误解析为纳秒时间戳）
        df.index = AdjFactorService._index_to_date_str(df.index)

        # 4. stack 提取非 1.0 值：where(≠1.0) 把 1.0→NaN，stack 转长表，dropna 丢 NaN
        #    结果只有除权事件行（每只股票一年几次），远小于 melt 全表
        #    pandas 2.1+ stack() 新实现不自动丢 NaN（与旧实现不同），需显式 dropna
        long_series = df.where(df != 1.0).stack().dropna()
        if long_series.empty:
            return pd.DataFrame(columns=["code", "trade_date", "adj_factor"])

        long_df = long_series.reset_index()
        long_df.columns = ["trade_date", "code", "adj_factor"]

        # 4. normalize trade_date → YYYY-MM-DD string
        long_df = AdjFactorService._normalize_trade_date_str(long_df)

        return long_df[["code", "trade_date", "adj_factor"]]

    @staticmethod
    def _index_to_date_str(idx) -> "pd.Index":
        """Index(交易日期) → YYYY-MM-DD 字符串 Index。处理 datetime/int/string 三种类型。

        int 类型（如 20240101）不能用 pd.to_datetime 直接解析（会被解释为纳秒时间戳，
        得到 1970-01-01 而非 2024-01-01），需先转字符串再用 format="%Y%m%d" 解析。
        """
        import pandas as pd
        if pd.api.types.is_datetime64_any_dtype(idx):
            return idx.strftime("%Y-%m-%d")
        if pd.api.types.is_integer_dtype(idx):
            return pd.to_datetime(
                idx.astype(str), format="%Y%m%d", errors="coerce"
            ).strftime("%Y-%m-%d")
        return pd.to_datetime(idx, errors="coerce").strftime("%Y-%m-%d")

    @staticmethod
    def _filter_wide_by_date(df: pd.DataFrame, start_dt, end_dt) -> pd.DataFrame:
        """宽表层面按 index(交易日期) 过滤行。start/end 为 None 时该侧不过滤。

        在宽表层面过滤 8687 行 index，比 melt 后过滤 43M 行长表省几个数量级内存。
        """
        import pandas as pd
        if (start_dt is None and end_dt is None) or df.empty:
            return df
        idx_str = AdjFactorService._index_to_date_str(df.index)
        idx_series = pd.Series(idx_str)
        mask = pd.Series(True, index=idx_series.index)
        if start_dt is not None:
            mask &= idx_series >= start_dt.strftime("%Y-%m-%d")
        if end_dt is not None:
            mask &= idx_series <= end_dt.strftime("%Y-%m-%d")
        return df[mask.values]

    @staticmethod
    def _melt_and_normalize(df: pd.DataFrame) -> pd.DataFrame:
        """SDK 宽表/长表 → code/trade_date/adj_factor 长表。非除权日过滤由 _filter_non_event_rows 完成。

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
        else:
            # 宽表形态：index=交易日期, columns=股票代码
            df = df.reset_index()
            date_col = df.columns[0]  # 按位置取日期列（index 可能无名）
            df = df.melt(id_vars=[date_col], var_name="code", value_name="adj_factor")
            df = df.rename(columns={date_col: "trade_date"})
        df = AdjFactorService._normalize_trade_date_str(df)
        df = AdjFactorService._filter_non_event_rows(df)
        return df[["code", "trade_date", "adj_factor"]]

    @staticmethod
    def _filter_non_event_rows(df: pd.DataFrame) -> pd.DataFrame:
        """过滤非除权事件行：dropna（兜底稀疏表）+ 排除 adj_factor==1.0（实测密集表非除权日值）。

        SDK get_adj_factor 返回**密集宽表**（每个交易日一行），非除权日的 adj_factor
        恒为 1.0（A 股无除权事件的标准约定）。实测单只股票全量 8687 行中仅 32 行
        是真除权事件，其余 8655 行均为 1.0。此处过滤掉 1.0 行，使返回结果与接口契约
        "每次除权除息事件一行"一致，同时把 melt/serialize 的内存峰值从 N×交易日降到
        N×事件数（约 1/270）。

        用精确 `!= 1.0` 而非 `np.isclose(., 1.0)`：实测样本有 0.9955、0.9791 等 <1.0
        的真除权事件（反向拆股/特殊股本变更），isclose 会误删这些边界事件。
        1.0 在 IEEE754 是精确表示，密集表所有非除权日值都是精确 1.0，比较可靠。
        """
        df = df.dropna(subset=["adj_factor"])
        df = df[df["adj_factor"] != 1.0]
        return df

    @staticmethod
    def _normalize_trade_date_str(df: pd.DataFrame) -> pd.DataFrame:
        """trade_date 列统一为 YYYY-MM-DD 字符串。

        datetime64 → dt.strftime；其他类型 → to_datetime 解析后 strftime。
        同 _truncate_kline_time_in_df（kline_service.py:131-141）模式：serialize_dataframe
        会把 datetime 转 ISO datetime，需在此显式截断为日期。
        """
        import pandas as pd
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
        import pandas as pd
        if df.empty:
            return df
        mask = pd.Series([True] * len(df), index=df.index)
        if start_dt is not None:
            mask &= df["trade_date"] >= start_dt.strftime("%Y-%m-%d")
        if end_dt is not None:
            mask &= df["trade_date"] <= end_dt.strftime("%Y-%m-%d")
        return df[mask]

"""FundDataService：ETF 份额/净值原始数据 Service 层（/etf/share、/etf/nav）。

职责边界：
- codes 缺省时经 gateway.get_code_list("EXTRA_ETF") 取全量 ETF（按日缓存）
- 深市份额 CHANGE_DATE 修正（snap 前一交易日）——数据准确性修复，非业务计算
- 拉取区间 end_date 后扩 10 天（覆盖深市 T+1 公告 / 净值 T+1 入库跨长假）
- 日期过滤 + 序列化

不负责（下游职责）：宽基 ETF 识别、净流入计算（share.diff() × nav）、share/nav join。

trade_date 语义（两接口统一 = 真实交易日，下游按 (code, trade_date) 直接 join）：
- share：沪市取 SDK CHANGE_DATE 原样（=T，可信）；深市 SDK CHANGE_DATE 实际填公告日
  T+1（与 ANN_DATE 相同），服务端用交易日历 snap 前一交易日还原为 T
- nav：取 SDK PRICE_DATE（净值计算日，沪深均准确，无需修正）

日期语义背景（深市份额）：
ETF 份额变动是 T 日收盘后的申赎结果，深市 T+1 才公告。若 T 为周五/节前最后一天，
公告落在下周一/节后（最长约 10 天），故拉取区间后扩 10 天再在 trade_date 上过滤。

详见 docs/API.md 的 /etf/share、/etf/nav 章节。
"""

from __future__ import annotations

import bisect
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.gateway import Gateway
from app.kline_service import to_sdk_date
from app.serializer import serialize_dataframe
from app.subscription_schedule import now_cn

logger = logging.getLogger("amazingdata.fund_data")

_DEFAULT_RANGE_DAYS = 30     # start_time/end_time 双缺省时的默认区间
_FETCH_END_EXTEND_DAYS = 10  # 深市 T+1 公告 / 净值 T+1 入库跨长假的最长覆盖
_RESULT_CACHE_MAX = 64       # 结果缓存上限，超限整体清空


def _to_date_str(s) -> "pd.Series":
    """将日期 Series 统一转为 YYYY-MM-DD 字符串。

    处理 datetime64/int/string 三种类型。int 格式（如 20240102）需用 format="%Y%m%d"
    解析，否则 pd.to_datetime 会将其视为纳秒时间戳 → 1970 年。NaT/NaN → NaN。
    """
    import pandas as pd
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d")
    elif pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    elif pd.api.types.is_float_dtype(s):
        # float 列通常因 int 列混入 NaN 升级而来（如 df.loc[1] = [None, ...]），
        # 非 NaN 值是整数日期（如 20240104.0），需先转 int 再用 format="%Y%m%d" 解析，
        # 否则 pd.to_datetime 会将其视为纳秒时间戳 → 1970 年。
        s_int = s.where(s.isna(), s.astype("Int64"))
        return pd.to_datetime(s_int, format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        return pd.to_datetime(s, errors="coerce").dt.strftime("%Y-%m-%d")


class FundDataService:
    """ETF 份额/净值原始数据 Service。

    缓存语义：
    - 查询结果按 (kind, frozenset(codes)|None, start_time, end_time) 缓存，
      TTL 由 cache_ttl_sec 控制（默认 300s）。份额/净值 T+1 更新，300s 无 freshness 风险。
      上限 64 条，超限整体清空。
    - 全量 ETF 清单按日缓存 (date_str, codes)，跨日自动失效重取。
    """

    def __init__(self, gateway: Gateway, cache_ttl_sec: int = 300):
        self._gw = gateway
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_lock = threading.Lock()
        self._list_cache: tuple[str, list[str]] | None = None
        self._result_cache: dict[tuple, tuple[float, list[dict]]] = {}

    def query_share(
        self,
        codes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """份额原始时序。返回 [{code, trade_date, share, ann_date}]，语义见模块 docstring。"""
        return self._query("share", codes, start_time, end_time)

    def query_nav(
        self,
        codes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """净值原始时序。返回 [{code, trade_date, nav}]，trade_date=PRICE_DATE 无需修正。"""
        return self._query("nav", codes, start_time, end_time)

    def _query(
        self,
        kind: str,
        codes: list[str] | None,
        start_time: str | None,
        end_time: str | None,
    ) -> list[dict]:
        # 1. 日期解析：双缺省默认近 30 天；start<=end 校验（ValueError → HTTP 422）
        if start_time is None and end_time is None:
            now_ts = now_cn()
            start_time = (now_ts - timedelta(days=_DEFAULT_RANGE_DAYS)).strftime("%Y-%m-%d")
            end_time = now_ts.strftime("%Y-%m-%d")
            logger.debug("fund_data 默认范围: %s..%s (近 %d 天)",
                         start_time, end_time, _DEFAULT_RANGE_DAYS)
        begin_date = to_sdk_date(start_time) if start_time else None
        end_date = to_sdk_date(end_time) if end_time else None
        if begin_date is not None and end_date is not None and begin_date > end_date:
            raise ValueError("start_time must not be later than end_time")

        # 2. 结果缓存：frozenset 消除 codes 顺序敏感；缺省用 None 哨兵
        cache_key = (kind, frozenset(codes) if codes else None, start_time, end_time)
        now_mono = time.monotonic()
        with self._cache_lock:
            hit = self._result_cache.get(cache_key)
            if hit is not None and now_mono - hit[0] <= self._cache_ttl_sec:
                logger.info("fund_data 结果缓存命中: kind=%s key=%s records=%d",
                            kind, cache_key[1:], len(hit[1]))
                return list(hit[1])

        # 3. codes 解析：缺省走全量 ETF 清单（按日缓存）
        resolved = list(codes) if codes else self._get_all_etf_codes()
        if not resolved:
            return []

        # 4. 拉取区间 end 后扩 10 天（深市 T+1 公告 / 净值 T+1 入库跨长假），
        #    拉完按 trade_date 过滤回用户区间。不做前扩（diff 是下游职责）。
        fetch_end = end_date
        if end_date is not None:
            fetch_end = int(
                (datetime.strptime(str(end_date), "%Y%m%d")
                 + timedelta(days=_FETCH_END_EXTEND_DAYS)).strftime("%Y%m%d")
            )

        # 5. 拉取（is_local/local_path 由 Gateway 内部从 Config 读取）
        t0 = time.monotonic()
        if kind == "share":
            data = self._gw.get_fund_share(
                resolved, is_local=False, begin_date=begin_date, end_date=fetch_end)
        else:
            data = self._gw.get_fund_nav(
                resolved, is_local=False, begin_date=begin_date, end_date=fetch_end)
        logger.info("fund_data fetch: kind=%s %.3fs codes=%d",
                    kind, time.monotonic() - t0, len(resolved))

        # 6. 规范化 + 过滤 + 序列化
        records = self._build_records(
            kind, resolved, data, start_time, end_time, self._gw.calendar)

        with self._cache_lock:
            if len(self._result_cache) >= _RESULT_CACHE_MAX:
                self._result_cache.clear()
            self._result_cache[cache_key] = (time.monotonic(), records)
        return records

    def _get_all_etf_codes(self) -> list[str]:
        """全量 ETF 代码清单，按日缓存（key=当日日期字符串）。"""
        today = now_cn().strftime("%Y-%m-%d")
        with self._cache_lock:
            if self._list_cache and self._list_cache[0] == today:
                return list(self._list_cache[1])
        codes = list(self._gw.get_code_list(security_type="EXTRA_ETF") or [])
        with self._cache_lock:
            self._list_cache = (today, codes)
        logger.info("fund_data 全量 ETF 清单: %d 只", len(codes))
        return list(codes)

    @staticmethod
    def _build_records(
        kind: str,
        codes: list[str],
        data_dict: dict[str, "pd.DataFrame"],
        start_str: str | None,
        end_str: str | None,
        calendar: list[int] | None,
    ) -> list[dict]:
        """规范化每只 ETF 的 DataFrame → 过滤用户区间 → 合并序列化。

        日期过滤统一用 YYYY-MM-DD 字符串比较；过滤前 dropna 丢弃日期缺失行。
        """
        import pandas as pd

        frames: list[pd.DataFrame] = []
        for code in codes:
            df = data_dict.get(code)
            if df is None or df.empty:
                continue
            if kind == "share":
                norm = FundDataService._normalize_share_df(df, code, calendar)
            else:
                norm = FundDataService._normalize_nav_df(df)
            # SDK 返回 CHANGE_DATE/PRICE_DATE 为 NaN 的行：丢弃，不参与日期比较
            norm = norm.dropna(subset=["trade_date"])
            if start_str is not None:
                norm = norm[norm["trade_date"] >= start_str]
            if end_str is not None:
                norm = norm[norm["trade_date"] <= end_str]
            if norm.empty:
                continue
            norm["code"] = code
            frames.append(norm)

        if not frames:
            return []
        combined = pd.concat(frames, ignore_index=True)
        cols = (["code", "trade_date", "share", "ann_date"] if kind == "share"
                else ["code", "trade_date", "nav"])
        return serialize_dataframe(combined[cols])

    @staticmethod
    def _normalize_share_df(
        df: "pd.DataFrame",
        code: str = "",
        calendar: list[int] | None = None,
    ) -> "pd.DataFrame":
        """规范化份额 DataFrame → trade_date/share/ann_date 三列，按 trade_date 排序。

        SDK 返回含 FUND_SHARE/CHANGE_DATE/ANN_DATE。
        - trade_date = 份额实际变动交易日 T；ann_date = 原始公告日（供审计）
        - 沪市：CHANGE_DATE=T（可信），ANN_DATE=T+1
        - 深市：CHANGE_DATE==ANN_DATE（都填公告日 T+1，CHANGE_DATE 不可信），
          用交易日历 snap 到小于 CHANGE_DATE 的最大交易日还原 T；
          无日历时退化为简单减 1 天（可能落周末，仅影响日期落点，记 warning）
        """
        import pandas as pd

        if "CHANGE_DATE" in df.columns:
            date_series = df["CHANGE_DATE"]
        elif "ANN_DATE" in df.columns:
            date_series = df["ANN_DATE"]
        else:
            date_series = df.index

        dates = _to_date_str(date_series)

        is_sz = code.endswith(".SZ")
        if is_sz and "CHANGE_DATE" in df.columns and "ANN_DATE" in df.columns:
            same_as_ann = (df["CHANGE_DATE"] == df["ANN_DATE"]).all()
            if same_as_ann:
                dt = pd.to_datetime(dates, format="%Y-%m-%d", errors="coerce")
                if calendar:
                    cal_sorted = sorted(set(calendar))

                    def snap_to_prev_trade(d):
                        if pd.isna(d):
                            return d
                        d_int = int(d.strftime("%Y%m%d"))
                        idx = bisect.bisect_left(cal_sorted, d_int)
                        if idx > 0:
                            return pd.to_datetime(str(cal_sorted[idx - 1]), format="%Y%m%d")
                        return d  # 日历里找不到，保持原值

                    dt = dt.apply(snap_to_prev_trade)
                else:
                    logger.warning(
                        "fund_data 深市份额修正无交易日历，退化为减 1 天: code=%s", code)
                    dt = dt - pd.Timedelta(days=1)
                dates = dt.dt.strftime("%Y-%m-%d")

        if "ANN_DATE" in df.columns:
            ann_dates = _to_date_str(df["ANN_DATE"])
        else:
            ann_dates = pd.Series([None] * len(df))

        result = pd.DataFrame({
            "trade_date": dates.values if hasattr(dates, "values") else dates,
            "share": df["FUND_SHARE"].values if "FUND_SHARE" in df.columns else None,
            "ann_date": ann_dates.values if hasattr(ann_dates, "values") else ann_dates,
        })
        return result.sort_values("trade_date").reset_index(drop=True)

    @staticmethod
    def _normalize_nav_df(df: "pd.DataFrame") -> "pd.DataFrame":
        """规范化净值 DataFrame → trade_date/nav 两列，按 trade_date 排序。

        SDK 返回含 UNIT_NAV/PRICE_DATE/ANN_DATE。
        trade_date = PRICE_DATE（净值计算日 T，沪深均准确，无需修正、无需 snap）。
        """
        import pandas as pd

        if "PRICE_DATE" in df.columns:
            date_series = df["PRICE_DATE"]
        elif "ANN_DATE" in df.columns:
            date_series = df["ANN_DATE"]
        else:
            date_series = df.index

        dates = _to_date_str(date_series)

        result = pd.DataFrame({
            "trade_date": dates.values if hasattr(dates, "values") else dates,
            "nav": df["UNIT_NAV"].values if "UNIT_NAV" in df.columns else None,
        })
        return result.sort_values("trade_date").reset_index(drop=True)

"""KlineService：HTTP 日期参数 → SDK 日期格式转换 + dict[code, DataFrame] 展平。

职责边界：
- 将 HTTP 的 ISO 日期/日期时间转为 SDK 要求的 8 位整型日期（YYYYMMDD）
- 调用 gateway.query_kline 获取 dict[code, DataFrame]
- 展平为 list[dict]，每条记录含 code 字段
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：字段重命名、单位换算、复权计算（由主项目 YAML field_map 完成）

性能：后处理（kline_time 截断 / UTC 转换）在 DataFrame 层用 pandas dt 访问器
向量化完成，避免逐条 dict 操作。NaT → strftime 返回 NaN → serializer 转 None。
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe

logger = logging.getLogger("amazingdata.kline")

# 分钟周期白名单。/minute 接口透传这些周期给 gateway，并附加 kline_time_utc 字段。
# 定义在业务层（kline_service）供 http_app 路由层复用，避免重复定义。
MINUTE_PERIODS: set[str] = {"min1", "min3", "min5", "min10", "min15", "min30", "min60", "min120"}

# 中国交易所本地时区（UTC+8）。SDK 返回的 kline_time 是 naive datetime，
# 视为该时区，转 UTC 后用于跨时区客户端。
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def to_sdk_date(iso: str) -> int:
    """ISO 日期/日期时间字符串 → SDK 8 位整型日期。

    支持：
    - 纯日期 "2024-01-01" → 20240101
    - 带时间 "2025-07-14T00:00:00" → 20250714（时间部分截断）
    - 带时区/毫秒 "2025-07-14T00:00:00.123+08:00" → 20250714

    使用 datetime.fromisoformat 统一解析（Python 3.11+ 支持完整 ISO 8601 子集），
    解析失败抛 ValueError，由上层转 422。
    """
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"date must be YYYY-MM-DD or ISO datetime (e.g. 2024-01-01 / 2024-01-01T00:00:00), got: {iso}"
        )
    return int(dt.strftime("%Y%m%d"))


class KlineService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(
        self,
        codes: list[str],
        start_time: str | None = None,
        end_time: str | None = None,
        period: str = "day",
    ) -> list[dict]:
        """查询 K 线数据，返回展平后的记录列表。

        start_time / end_time 可选；为 None 时不传给 SDK，由 SDK 使用默认区间
        （begin_date 默认 20240101，end_date 默认 20991231）。
        仅当两者都提供时校验 start_time <= end_time（按解析后的日期比较，
        不受时间部分精度影响）。
        period 默认 "day"（/daily 接口使用）；/minute 接口传入 "min1"~"min120"
        等分钟周期，透传给 gateway。
        空结果返回 []（HTTP 层包装为 {"data": []}，HTTP 200）。

        minute 周期默认区间优化：start_time/end_time 均未传时，begin_date 默认
        设为近一年（当前日期前 365 天），避免 SDK 默认 20240101 导致返回 2.5 年
        分钟数据（约 30 万条，gateway 查询 20s+）。day 等周期仍用 SDK 默认区间。
        """
        if period in MINUTE_PERIODS and start_time is None and end_time is None:
            # 分钟K不传日期时默认近一年，end_date 用 None 让 SDK 取到最新
            now = datetime.now()
            begin_date = int((now - timedelta(days=365)).strftime("%Y%m%d"))
            end_date = None
            logger.info("minute default range: begin_date=%s (last 365 days)", begin_date)
        else:
            begin_date = to_sdk_date(start_time) if start_time else None
            end_date = to_sdk_date(end_time) if end_time else None
        if begin_date is not None and end_date is not None and begin_date > end_date:
            raise ValueError("start_time must not be later than end_time")

        # 计时日志：区分 SDK 查询耗时与后处理（flatten + 序列化）耗时，
        # 便于定位 /minute 慢请求的瓶颈是在 SDK 服务端还是本地序列化。
        t0 = time.monotonic()
        result = self._gw.query_kline(codes, begin_date, end_date, period)
        t1 = time.monotonic()
        records = self._flatten(result, period)
        t2 = time.monotonic()
        logger.info(
            "kline query: gateway=%.3fs flatten=%.3fs period=%s codes=%d records=%d",
            t1 - t0, t2 - t1, period, len(codes), len(records),
        )
        return records

    @staticmethod
    def _flatten(result: dict[str, pd.DataFrame], period: str = "day") -> list[dict]:
        """将 dict[code, DataFrame] 展平为 list[dict]。

        - 跳过 None 或空 DataFrame
        - 若 DataFrame 缺少 code 列，用 dict 的 key 补上
        - 后处理（日K截断 / 分钟K加UTC）在 DataFrame 层向量化完成，避免逐条 dict 操作
        - 索引重置和类型序列化委托给 serialize_dataframe
        """
        records: list[dict] = []
        for code, df in result.items():
            if df is None or df.empty:
                continue
            df = df.copy()
            if "code" not in df.columns:
                df["code"] = code
            # DataFrame 层后处理（向量化）：kline_time 列存在时按周期转换
            if "kline_time" in df.columns:
                if period == "day":
                    _truncate_kline_time_in_df(df)
                elif period in MINUTE_PERIODS:
                    _add_kline_time_utc_to_df(df)
            records.extend(serialize_dataframe(df))
        return records


def _truncate_kline_time_in_df(df: pd.DataFrame) -> None:
    """日 K：kline_time datetime 列原地替换为 yyyy-MM-dd 字符串（向量化）。

    kline_time 为 datetime64[us]（见 probe-report.json），strftime("%Y-%m-%d")
    向量化转换为日期字符串。NaT → NaN，经 serializer 转为 null。
    非 datetime 类型时跳过（serialize 兜底处理）。
    """
    try:
        df["kline_time"] = df["kline_time"].dt.strftime("%Y-%m-%d")
    except (AttributeError, TypeError):
        pass


def _add_kline_time_utc_to_df(df: pd.DataFrame) -> None:
    """分钟 K：在 DataFrame 层添加 kline_time_utc 列（向量化）。

    kline_time 是 naive datetime（UTC+8），tz_localize 视为 UTC+8 后 tz_convert
    转 UTC，strftime 为 "yyyy-MM-ddTHH:mm:ss"。列插入到 kline_time 之后，
    保持时间字段相邻。NaT 经 tz_localize 保持 NaT → strftime 返回 NaN →
    serializer 转 null。非 datetime 类型或列缺失时跳过。
    """
    try:
        utc = df["kline_time"].dt.tz_localize(_SHANGHAI_TZ).dt.tz_convert(timezone.utc)
        col_idx = df.columns.get_loc("kline_time")
        df.insert(col_idx + 1, "kline_time_utc", utc.dt.strftime("%Y-%m-%dT%H:%M:%S"))
    except (AttributeError, TypeError, KeyError):
        pass

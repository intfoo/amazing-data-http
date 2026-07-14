"""KlineService：HTTP 日期参数 → SDK 日期格式转换 + dict[code, DataFrame] 展平。

职责边界：
- 将 HTTP 的 ISO 日期/日期时间转为 SDK 要求的 8 位整型日期（YYYYMMDD）
- 调用 gateway.query_kline 获取 dict[code, DataFrame]
- 展平为 list[dict]，每条记录含 code 字段
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：字段重命名、单位换算、复权计算（由主项目 YAML field_map 完成）
"""

from datetime import datetime

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe


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
        symbols: list[str],
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """查询日 K 数据，返回展平后的记录列表。

        start_time / end_time 可选；为 None 时不传给 SDK，由 SDK 使用默认区间
        （begin_date 默认 20240101，end_date 默认 20991231）。
        仅当两者都提供时校验 start_time <= end_time（按解析后的日期比较，
        不受时间部分精度影响）。
        首期固定使用 "day" 周期，不接受请求体中的任意周期参数。
        空结果返回 []（HTTP 层包装为 {"data": []}，HTTP 200）。
        """
        begin_date = to_sdk_date(start_time) if start_time else None
        end_date = to_sdk_date(end_time) if end_time else None
        if begin_date is not None and end_date is not None and begin_date > end_date:
            raise ValueError("start_time must not be later than end_time")
        result = self._gw.query_kline(symbols, begin_date, end_date, "day")
        return self._flatten(result)

    @staticmethod
    def _flatten(result: dict[str, pd.DataFrame]) -> list[dict]:
        """将 dict[code, DataFrame] 展平为 list[dict]。

        - 跳过 None 或空 DataFrame
        - 若 DataFrame 缺少 code 列，用 dict 的 key 补上
        - 索引重置和类型序列化委托给 serialize_dataframe
        """
        records: list[dict] = []
        for code, df in result.items():
            if df is None or df.empty:
                continue
            df = df.copy()
            if "code" not in df.columns:
                df["code"] = code
            records.extend(serialize_dataframe(df))
        return records

"""KlineService：HTTP 日期参数 → SDK 日期格式转换 + dict[code, DataFrame] 展平。

职责边界：
- 将 HTTP 的 ISO 日期（YYYY-MM-DD）转为 SDK 要求的 8 位整型日期（YYYYMMDD）
- 调用 gateway.query_kline 获取 dict[code, DataFrame]
- 展平为 list[dict]，每条记录含 code 字段
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：字段重命名、单位换算、复权计算（由主项目 YAML field_map 完成）
"""

import re
from datetime import datetime

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_sdk_date(iso: str) -> int:
    """ISO 日期字符串 → SDK 8 位整型日期。如 "2024-01-01" → 20240101。

    先用正则校验格式，再用 strptime 校验合法性（如 02-31 会报错）。
    """
    if not _ISO_DATE.match(iso):
        raise ValueError(f"date must be YYYY-MM-DD, got: {iso}")
    datetime.strptime(iso, "%Y-%m-%d")
    return int(iso.replace("-", ""))


class KlineService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(self, symbols: list[str], start_time: str, end_time: str) -> list[dict]:
        """查询日 K 数据，返回展平后的记录列表。

        首期固定使用 "day" 周期，不接受请求体中的任意周期参数。
        空结果返回 []（HTTP 层包装为 {"data": []}，HTTP 200）。
        """
        begin_date = to_sdk_date(start_time)
        end_date = to_sdk_date(end_time)
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

import re
from datetime import datetime

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_sdk_date(iso: str) -> int:
    if not _ISO_DATE.match(iso):
        raise ValueError(f"date must be YYYY-MM-DD, got: {iso}")
    datetime.strptime(iso, "%Y-%m-%d")
    return int(iso.replace("-", ""))


class KlineService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(self, symbols: list[str], start_time: str, end_time: str) -> list[dict]:
        begin_date = to_sdk_date(start_time)
        end_date = to_sdk_date(end_time)
        result = self._gw.query_kline(symbols, begin_date, end_date, "day")
        return self._flatten(result)

    @staticmethod
    def _flatten(result: dict[str, pd.DataFrame]) -> list[dict]:
        records: list[dict] = []
        for code, df in result.items():
            if df is None or df.empty:
                continue
            df = df.copy()
            if "code" not in df.columns:
                df["code"] = code
            records.extend(serialize_dataframe(df))
        return records

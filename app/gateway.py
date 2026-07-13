from typing import Any, Protocol, runtime_checkable

import pandas as pd


PERIOD_MAP: dict[str, str] = {
    "day": "day",
    "min1": "min1",
    "min3": "min3",
    "min5": "min5",
    "min10": "min10",
    "min15": "min15",
    "min30": "min30",
    "min60": "min60",
    "min120": "min120",
    "week": "week",
    "month": "month",
    "season": "season",
    "year": "year",
}


@runtime_checkable
class Gateway(Protocol):
    def login(self) -> None: ...
    def logout(self) -> None: ...
    def is_ready(self) -> bool: ...
    def query_kline(
        self,
        symbols: list[str],
        begin_date: int,
        end_date: int,
        period: str,
    ) -> dict[str, pd.DataFrame]: ...


class GatewayError(Exception):
    pass


class GatewayNotReadyError(GatewayError):
    pass


class GatewayQueryError(GatewayError):
    pass

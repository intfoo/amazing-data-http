from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.gateway import Gateway, GatewayNotReadyError, GatewayQueryError

_UNSET = object()


class FakeGateway:
    def __init__(self, ready: bool = True, result: dict[str, pd.DataFrame] | None = _UNSET):
        self._ready = ready
        self._result = result if result is not _UNSET else {}
        self._logged_in = ready
        self.login_called = 0
        self.logout_called = 0
        self.query_calls: list[dict] = []

    def login(self) -> None:
        self.login_called += 1
        self._logged_in = True
        self._ready = True

    def logout(self) -> None:
        self.logout_called += 1
        self._logged_in = False
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    def query_kline(self, symbols, begin_date, end_date, period):
        self.query_calls.append({
            "symbols": symbols,
            "begin_date": begin_date,
            "end_date": end_date,
            "period": period,
        })
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        if self._result is None:
            raise GatewayQueryError("fake query failed")
        return self._result


def make_daily_df(code: str = "000001.SZ", rows: int = 1) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "code": [code] * rows,
            "open": [10.2] * rows,
            "high": [10.45] * rows,
            "low": [10.1] * rows,
            "close": [10.3] * rows,
            "volume": [np.int64(1234567)] * rows,
            "amount": [12700000.0] * rows,
        },
        index=pd.Index(dates, name="trade_time"),
    )


@pytest.fixture
def fake_gateway():
    return FakeGateway(ready=True)


@pytest.fixture
def fake_gateway_with_data():
    return FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})

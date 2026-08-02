from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.gateway import Gateway, GatewayNotReadyError, GatewayQueryError

_UNSET = object()


class FakeGateway:
    def __init__(self, ready: bool = True, result: dict[str, pd.DataFrame] | None = _UNSET,
                 adj_factor_result: pd.DataFrame | None = None,
                 calendar: list[int] | None = None,
                 code_info_result: pd.DataFrame | None = None,
                 fund_share_result: dict[str, pd.DataFrame] | None = None,
                 fund_nav_result: dict[str, pd.DataFrame] | None = None):
        self._ready = ready
        self._result = result if result is not _UNSET else {}
        self._adj_factor_result = adj_factor_result
        self._logged_in = ready
        self.login_called = 0
        self.logout_called = 0
        self.query_calls: list[dict] = []
        self.adj_factor_query_calls: list[dict] = []
        self._code_info_result = code_info_result
        self._fund_share_result = fund_share_result or {}
        self._fund_nav_result = fund_nav_result or {}
        self.fund_share_calls: list[dict] = []
        self.fund_nav_calls: list[dict] = []
        self._code_list = ["000001.SZ", "600000.SH"]
        self._index_code_list = ["000001.SH", "399001.SZ"]
        self.sub_start_called = 0
        self.sub_stop_called = 0
        self._sub_code_list = None
        self._calendar = calendar

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

    @property
    def calendar(self) -> list[int] | None:
        return self._calendar

    def query_kline(self, codes, begin_date, end_date, period):
        self.query_calls.append({
            "codes": codes,
            "begin_date": begin_date,
            "end_date": end_date,
            "period": period,
        })
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        if self._result is None:
            raise GatewayQueryError("fake query failed")
        return self._result

    def get_code_list(self, security_type: str = "EXTRA_STOCK_A"):
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        if security_type == "EXTRA_INDEX_A":
            return list(self._index_code_list)
        return list(self._code_list)

    def get_realtime_code_list(self) -> list[str]:
        return self.get_code_list("EXTRA_STOCK_A") + self.get_code_list("EXTRA_INDEX_A")

    def query_snapshot(self, codes, trade_date=None, begin_time=None, end_time=None):
        """FakeGateway 快照查询：未就绪抛 GatewayNotReadyError，否则返回空 dict。"""
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        self.snapshot_query_calls = getattr(self, "snapshot_query_calls", [])
        self.snapshot_query_calls.append({
            "codes": codes, "trade_date": trade_date,
            "begin_time": begin_time, "end_time": end_time,
        })
        return {}

    def start_snapshot_subscription(self, code_list, on_data, on_error=None):
        self.sub_start_called += 1
        self._sub_code_list = code_list

    def stop_subscription(self):
        self.sub_stop_called += 1

    def get_adj_factor(self, codes):
        """FakeGateway 除权因子查询：记录调用，未就绪抛 GatewayNotReadyError。"""
        self.adj_factor_query_calls.append({"codes": codes})
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        if self._adj_factor_result is None:
            return pd.DataFrame()
        return self._adj_factor_result

    def get_code_info(self, security_type="EXTRA_STOCK_A"):
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        return self._code_info_result

    def get_fund_share(self, codes, is_local=False, begin_date=None, end_date=None):
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        self.fund_share_calls.append({
            "codes": codes, "is_local": is_local,
            "begin_date": begin_date, "end_date": end_date,
        })
        return self._fund_share_result

    def get_fund_nav(self, codes, is_local=False, begin_date=None, end_date=None):
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        self.fund_nav_calls.append({
            "codes": codes, "is_local": is_local,
            "begin_date": begin_date, "end_date": end_date,
        })
        return self._fund_nav_result


def make_daily_df(code: str = "000001.SZ", rows: int = 1) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "code": [code] * rows,
            "kline_time": list(dates),  # Timestamp，反映真实 SDK 返回结构（见 probe-report.json）
            "open": [10.2] * rows,
            "high": [10.45] * rows,
            "low": [10.1] * rows,
            "close": [10.3] * rows,
            "volume": [np.int64(1234567)] * rows,
            "amount": [12700000.0] * rows,
        },
        index=pd.Index(dates, name="trade_time"),
    )


def make_adj_factor_df() -> pd.DataFrame:
    """模拟 SDK get_adj_factor 返回的**密集宽表**：index=交易日期, columns=股票代码。

    SDK 实测返回密集表（每个交易日一行），非除权日 adj_factor=1.0（A 股无除权事件的标准约定），
    而非稀疏表（NaN 表示非除权日）。AdjFactorService._filter_non_event_rows 会过滤掉 1.0 行。
    与 SDK 手册 3.5.2.6 输出格式一致，且符合实际 SDK 行为（实测 8687 行/股，99.6% 为 1.0）。
    """
    dates = pd.date_range("2024-05-30", periods=2, freq="D")
    return pd.DataFrame(
        {
            "000001.SZ": [1.05, 1.0],   # 5-30 除权事件, 5-31 非除权日
            "600000.SH": [1.0, 1.10],   # 5-30 非除权日, 5-31 除权事件
        },
        index=pd.Index(dates, name="trade_date"),
    )


@pytest.fixture
def fake_gateway():
    return FakeGateway(ready=True)


@pytest.fixture
def fake_gateway_with_data():
    return FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})

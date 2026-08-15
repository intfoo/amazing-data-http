"""POST /etf/share、/etf/nav 路由级测试。"""

from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from tests.conftest import FakeGateway


def make_test_app(gateway=None, auth_token="", auth_required=False):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
    )
    if gateway is None:
        gateway = FakeGateway(ready=True)
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)


def _share_df():
    return pd.DataFrame({
        "CHANGE_DATE": [20240103, 20240104],
        "FUND_SHARE": [100.0, 101.0],
        "ANN_DATE": [20240104, 20240105],
    })


def _nav_df():
    return pd.DataFrame({
        "PRICE_DATE": [20240103, 20240104],
        "UNIT_NAV": [4.1, 4.2],
    })


CAL = [20240102, 20240103, 20240104, 20240105, 20240108]


def test_etf_share_basic():
    gw = FakeGateway(ready=True, calendar=CAL,
                     fund_share_result={"510300.SH": _share_df()})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/share", json={
        "codes": ["510300.SH"], "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 2
    assert set(rows[0].keys()) == {"code", "trade_date", "share", "ann_date"}
    assert rows[0]["trade_date"] == "2024-01-03"


def test_etf_share_codes_optional():
    """codes 缺省 → 全量 ETF 清单，200。"""
    gw = FakeGateway(ready=True, calendar=CAL,
                     etf_code_list=["510300.SH"],
                     fund_share_result={"510300.SH": _share_df()})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/share", json={
        "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert [r["code"] for r in resp.json()["data"]] == ["510300.SH"] * 2


def test_etf_nav_basic():
    gw = FakeGateway(ready=True, calendar=CAL,
                     fund_nav_result={"510300.SH": _nav_df()})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/nav", json={
        "codes": ["510300.SH"], "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 2
    assert set(rows[0].keys()) == {"code", "trade_date", "nav"}
    assert rows[1]["nav"] == 4.2


def test_etf_share_start_after_end_422():
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/share", json={
        "codes": ["510300.SH"], "start_time": "2024-02-01", "end_time": "2024-01-01",
    })
    assert resp.status_code == 422


def test_etf_nav_empty_result():
    gw = FakeGateway(ready=True, calendar=CAL, fund_nav_result={})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/nav", json={
        "codes": ["510300.SH"], "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_etf_net_inflow_removed():
    """旧接口 /etf/net_inflow 已下线：404 或 405。"""
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={})
    assert resp.status_code in (404, 405)

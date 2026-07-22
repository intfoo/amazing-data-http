"""AdjFactorService 单元测试 + POST /adj_factor HTTP 端到端测试。"""
from datetime import datetime

import pandas as pd
import pytest

from app.adj_factor_service import AdjFactorService
from app.gateway import GatewayNotReadyError, GatewayQueryError
from tests.conftest import FakeGateway, make_adj_factor_df


# ========== Service 单元测试 ==========

def test_query_melts_wide_dataframe():
    """SDK 宽表 → 正确长表 [{code, trade_date, adj_factor}]。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"])
    assert len(data) == 2
    codes = {row["code"] for row in data}
    assert codes == {"000001.SZ", "600000.SH"}
    for row in data:
        assert set(row.keys()) == {"code", "trade_date", "adj_factor"}
        assert row["trade_date"] in ("2024-05-30", "2024-05-31")


def test_query_filters_non_event_rows_adj_factor_eq_1():
    """密集宽表中 adj_factor==1.0 的非除权日行被过滤，只保留真除权事件。

    SDK 实测返回密集表（非除权日 adj_factor=1.0，非 NaN）。AdjFactorService._filter_non_event_rows
    先 dropna（兜底）再过滤 1.0，使返回结果与契约"每次除权除息事件一行"一致。
    """
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"])
    # 密集表 2 日期 × 2 代码 = 4 行，2 行 1.0 + 2 行非 1.0，过滤后剩 2 行
    assert len(data) == 2
    for row in data:
        assert row["adj_factor"] != 1.0


def test_query_date_filter_start_only():
    """start_time 过滤掉早于该日期的行。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"], start_time="2024-05-31")
    assert len(data) == 1
    assert data[0]["trade_date"] == "2024-05-31"
    assert data[0]["code"] == "600000.SH"


def test_query_date_filter_end_only():
    """end_time 过滤掉晚于该日期的行。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"], end_time="2024-05-30")
    assert len(data) == 1
    assert data[0]["trade_date"] == "2024-05-30"
    assert data[0]["code"] == "000001.SZ"


def test_query_date_filter_both_sides():
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ"], start_time="2024-05-30", end_time="2024-05-30")
    assert len(data) == 1
    assert data[0]["code"] == "000001.SZ"


def test_query_empty_dataframe():
    """SDK 返回空 DataFrame → 返回 []。"""
    gw = FakeGateway(ready=True, adj_factor_result=pd.DataFrame())
    svc = AdjFactorService(gw)
    assert svc.query(["000001.SZ"]) == []


def test_query_no_result_returns_empty_list():
    """FakeGateway adj_factor_result=None → 空结果。"""
    gw = FakeGateway(ready=True, adj_factor_result=None)
    svc = AdjFactorService(gw)
    assert svc.query(["000001.SZ"]) == []


def test_query_reversed_dates_raises():
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    with pytest.raises(ValueError, match="start_time must not be later than end_time"):
        svc.query(["000001.SZ"], "2024-05-31", "2024-05-30")


def test_query_invalid_date_format_raises():
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    with pytest.raises(ValueError):
        svc.query(["000001.SZ"], "2024/05/30")


def test_query_iso_datetime_accepted():
    """ISO datetime 应被接受并截断为日期比较。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ"], "2024-05-30T00:00:00", "2024-05-30T23:59:59")
    assert len(data) == 1


def test_query_sdk_not_ready():
    gw = FakeGateway(ready=False)
    svc = AdjFactorService(gw)
    with pytest.raises(GatewayNotReadyError):
        svc.query(["000001.SZ"])


def test_query_optional_dates_none():
    """不传日期 → 不过滤，返回全部事件。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"])
    assert len(data) == 2


def test_query_passes_codes_to_gateway():
    """codes 原样传给 gateway.get_adj_factor。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    svc.query(["000001.SZ", "600000.SH"])
    assert gw.adj_factor_query_calls[0]["codes"] == ["000001.SZ", "600000.SH"]


def test_query_dense_table_filters_all_unit_rows():
    """密集宽表（多日期多 1.0 行）只保留真除权事件，验证 1.0 过滤不误删 <1.0 边界事件。

    SDK 实测样本含 0.9955、0.9791 等 <1.0 的真除权事件（反向拆股/股本变更），
    必须用精确 != 1.0 而非 np.isclose，避免误删这些边界事件。
    """
    # 模拟真实 SDK 密集表：5 个交易日，000001.SZ 有 2 个除权事件（1.05, 0.9955），其余 1.0
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    df = pd.DataFrame(
        {
            "000001.SZ": [1.0, 1.05, 1.0, 0.9955, 1.0],
        },
        index=pd.Index(dates, name="trade_date"),
    )
    gw = FakeGateway(ready=True, adj_factor_result=df)
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ"])
    assert len(data) == 2  # 1.05 和 0.9955，1.0 行被过滤
    values = sorted(r["adj_factor"] for r in data)
    assert values == [0.9955, 1.05]  # 验证 <1.0 事件未被误删


def test_query_sparse_table_dropna_path():
    """稀疏宽表（NaN 表示非除权日）也能被正确过滤（dropna 兜底 + != 1.0）。

    SDK 实测返回密集表（1.0），但 _filter_non_event_rows 保留 dropna 兜底稀疏表。
    此测试验证稀疏表路径仍工作（防御性，SDK 未来可能改回稀疏表）。
    """
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {
            "000001.SZ": [1.05, None, 1.0],  # 除权事件, NaN(稀疏), 非除权日
        },
        index=pd.Index(dates, name="trade_date"),
    )
    gw = FakeGateway(ready=True, adj_factor_result=df)
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ"])
    # dropna 过滤 NaN 行，!= 1.0 过滤非除权日，只剩 1.05
    assert len(data) == 1
    assert data[0]["adj_factor"] == 1.05


# ========== HTTP 端到端测试 ==========

from app.config import Config
from app.http_app import create_app
from fastapi.testclient import TestClient


def make_test_app(gateway=None, auth_token="", auth_required=False):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
    )
    if gateway is None:
        gateway = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)


def test_adj_factor_http_success():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ", "600000.SH"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert len(body["data"]) == 2
    row = body["data"][0]
    assert set(row.keys()) == {"code", "trade_date", "adj_factor"}


def test_adj_factor_http_field_names_neutral():
    """响应字段为 code/trade_date/adj_factor，非 stocker 的 symbol/ex_factor。"""
    client = make_test_app()
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert "code" in row
    assert "trade_date" in row
    assert "adj_factor" in row
    assert "symbol" not in row
    assert "ex_factor" not in row


def test_adj_factor_http_date_filter():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ", "600000.SH"],
        "start_time": "2024-05-31",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["trade_date"] == "2024-05-31"


def test_adj_factor_http_empty_codes_422():
    client = make_test_app()
    resp = client.post("/adj_factor", json={"codes": []})
    assert resp.status_code == 422


def test_adj_factor_http_reversed_dates_422():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-05-31",
        "end_time": "2024-05-30",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_adj_factor_http_invalid_date_422():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ"],
        "start_time": "2024/05/30",
    })
    assert resp.status_code == 422


def test_adj_factor_http_sdk_not_ready_503():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_adj_factor_http_empty_result_200():
    gw = FakeGateway(ready=True, adj_factor_result=None)
    client = make_test_app(gateway=gw)
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_adj_factor_http_optional_dates_missing():
    client = make_test_app()
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_adj_factor_http_iso_datetime_accepted():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-05-30T00:00:00",
        "end_time": "2024-05-30T23:59:59",
    })
    assert resp.status_code == 200


def test_adj_factor_http_error_has_request_id():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert "request_id" in resp.json()["error"]
    assert resp.headers.get("X-Request-ID") is not None

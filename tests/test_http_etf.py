"""POST /etf/net_inflow HTTP 端到端测试。"""
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


def make_code_info_df() -> pd.DataFrame:
    """模拟 SDK get_code_info('EXTRA_ETF') 返回。"""
    return pd.DataFrame(
        {"symbol": ["沪深300ETF", "中证500ETF"]},
        index=pd.Index(["510300.SH", "510500.SH"], name="code"),
    )


def make_share_df() -> pd.DataFrame:
    """模拟 SDK get_fund_share 返回：3 天数据。"""
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    return pd.DataFrame(
        {
            "FUND_SHARE": [100.0, 100.0, 120.0],
            "CHANGE_DATE": [20240102, 20240103, 20240104],
            "ANN_DATE": [20240102, 20240103, 20240104],
        },
        index=pd.Index(dates, name="date"),
    )


def make_nav_df() -> pd.DataFrame:
    """模拟 SDK get_fund_nav 返回：3 天数据。"""
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    return pd.DataFrame(
        {
            "UNIT_NAV": [1.0, 1.0, 1.05],
            "PRICE_DATE": [20240102, 20240103, 20240104],
            "ANN_DATE": [20240102, 20240103, 20240104],
        },
        index=pd.Index(dates, name="date"),
    )


def make_full_gateway(ready: bool = True) -> FakeGateway:
    """构造注入完整 mock 数据的 FakeGateway。"""
    return FakeGateway(
        ready=ready,
        code_info_result=make_code_info_df(),
        fund_share_result={
            "510300.SH": make_share_df(),
            "510500.SH": make_share_df(),
        },
        fund_nav_result={
            "510300.SH": make_nav_df(),
            "510500.SH": make_nav_df(),
        },
    )


def test_etf_net_inflow_200():
    """正常请求返回数据。"""
    gw = make_full_gateway()
    client = make_test_app(gateway=gw)
    # 传日期匹配 mock 数据(2024-01-02 ~ 2024-01-04)
    # 不传日期时默认近 30 天,mock 数据日期会被过滤掉
    resp = client.post("/etf/net_inflow", json={
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    # 2 只 ETF × 3 天 = 6 条记录
    assert len(body["data"]) == 6
    row = body["data"][0]
    assert set(row.keys()) == {
        "code", "name", "date", "share", "nav",
        "net_inflow_share", "net_inflow_amount",
    }


def test_etf_net_inflow_with_dates():
    """带日期参数的正常请求。"""
    gw = make_full_gateway()
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={
        "start_time": "2024-01-03",
        "end_time": "2024-01-04",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 2 只 ETF × 2 天 = 4 条
    assert len(data) == 4
    for row in data:
        assert row["date"] >= "2024-01-03"
        assert row["date"] <= "2024-01-04"


def test_etf_net_inflow_empty():
    """宽基为空返回 {"data": []}。"""
    # 非宽基 ETF
    code_info_df = pd.DataFrame(
        {"symbol": ["医药ETF", "券商ETF"]},
        index=pd.Index(["159999.SZ", "512000.SH"], name="code"),
    )
    gw = FakeGateway(ready=True, code_info_result=code_info_df)
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={})
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_etf_net_inflow_invalid_date():
    """日期格式错误 → 422。"""
    gw = make_full_gateway()
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={
        "start_time": "2024/01/02",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_etf_net_inflow_start_after_end():
    """start > end → 422。"""
    gw = make_full_gateway()
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={
        "start_time": "2024-01-04",
        "end_time": "2024-01-02",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_etf_net_inflow_sdk_not_ready():
    """SDK 未就绪 → 503。"""
    gw = make_full_gateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_etf_net_inflow_optional_dates_missing():
    """不传 start_time/end_time → 200。"""
    gw = make_full_gateway()
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={})
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_etf_net_inflow_error_has_request_id():
    """错误响应含 request_id 和 X-Request-ID header。"""
    gw = make_full_gateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={})
    assert resp.status_code == 503
    assert "request_id" in resp.json()["error"]
    assert resp.headers.get("X-Request-ID") is not None


def test_etf_net_inflow_iso_datetime_accepted():
    """ISO datetime 格式应被接受。"""
    gw = make_full_gateway()
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={
        "start_time": "2024-01-02T00:00:00",
        "end_time": "2024-01-02T23:59:59",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2  # 2 只 ETF × 1 天
    for row in data:
        assert row["date"] == "2024-01-02"

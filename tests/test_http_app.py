import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from tests.conftest import FakeGateway, make_daily_df


def make_test_app(gateway=None):
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    if gateway is None:
        gateway = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)


def test_daily_success():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-02",
        "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"
    assert body["data"][0]["close"] == 10.3


def test_daily_iso_datetime_format():
    """ISO datetime 格式应被接受并截断为日期。"""
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-02T00:00:00",
        "end_time": "2024-01-02T23:59:59",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"


def test_daily_optional_dates_both_missing():
    """不传 start_time/end_time 应使用 SDK 默认区间，返回 200。"""
    client = make_test_app()
    resp = client.post("/daily", json={"symbols": ["000001.SZ"]})
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_daily_optional_dates_one_side():
    """只传一端时间应接受，另一端用 SDK 默认。"""
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
    })
    assert resp.status_code == 200

    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "end_time": "2024-12-31",
    })
    assert resp.status_code == 200


def test_daily_empty_result():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_daily_empty_symbols():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": [],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 422


def test_daily_reversed_dates():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-31",
        "end_time": "2024-01-01",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_daily_invalid_date_format():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2025/07/14",  # 斜杠分隔非 ISO
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 422


def test_daily_sdk_not_ready():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_daily_sdk_query_failed():
    gw = FakeGateway(ready=True, result=None)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "SDK_QUERY_FAILED"


def test_daily_error_has_request_id():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert "request_id" in resp.json()["error"]
    assert resp.headers.get("X-Request-ID") is not None


def test_health_ok():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sdk"] == "ready"


def test_health_degraded_when_not_ready():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["sdk"] == "not_ready"


def test_health_no_secrets_in_response():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    body_text = resp.text
    assert "password" not in body_text.lower()


def test_daily_validation_error_envelope():
    """422 校验失败应返回统一错误信封（含 code/errors/request_id）。

    时间参数已改为可选，这里用空 body 触发 symbols 必填校验失败。
    """
    client = make_test_app()
    resp = client.post("/daily", json={})  # 缺 symbols
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "errors" in body["error"]
    assert "request_id" in body["error"]

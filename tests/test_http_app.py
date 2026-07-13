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
        "start_time": "20240101",
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

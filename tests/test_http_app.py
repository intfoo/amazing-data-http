import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from tests.conftest import FakeGateway, make_daily_df


def make_test_app(gateway=None, auth_token="", auth_required=False,
                  subscription_open="09:00", subscription_close="15:20"):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
        subscription_open=subscription_open, subscription_close=subscription_close,
    )
    if gateway is None:
        gateway = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)


def _today_cal():
    return [int(datetime.datetime.now().strftime("%Y%m%d"))]


def _wait_scheduler(app, timeout=5):
    """等待订阅调度器完成首次 tick（启动或跳过订阅）。"""
    scheduler = getattr(app.state, "subscription_scheduler", None)
    if scheduler:
        scheduler._first_tick_done.wait(timeout=timeout)


def test_daily_success():
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-02",
        "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"
    assert body["data"][0]["close"] == 10.3


def test_daily_kline_time_is_date_only():
    """daily 接口的 kline_time 应返回 yyyy-MM-dd 格式（不含时分秒）。"""
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-02",
        "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["kline_time"] == "2024-01-02"


def test_minute_kline_time_keeps_full_datetime():
    """minute 接口的 kline_time 保留完整 datetime（含时分秒）。"""
    client = make_test_app()
    resp = client.post("/minute", json={
        "codes": ["000001.SZ"], "period": "min5",
        "start_time": "2024-01-02", "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["kline_time"] == "2024-01-02T00:00:00"


def test_minute_kline_time_utc_present():
    """minute 接口响应应包含 kline_time_utc 字段（UTC，带 Z 后缀）。

    make_daily_df 的 kline_time 是 00:00:00（UTC+8），转 UTC 为前一天 16:00Z。
    """
    client = make_test_app()
    resp = client.post("/minute", json={
        "codes": ["000001.SZ"], "period": "min5",
        "start_time": "2024-01-02", "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["kline_time_utc"] == "2024-01-01T16:00:00"


def test_daily_no_kline_time_utc_field():
    """daily 接口响应不应包含 kline_time_utc 字段。"""
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-02", "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert "kline_time_utc" not in row


def test_daily_iso_datetime_format():
    """ISO datetime 格式应被接受并截断为日期。"""
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
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
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200


def test_daily_optional_dates_one_side():
    """只传一端时间应接受，另一端用 SDK 默认。"""
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-01",
    })
    assert resp.status_code == 200

    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "end_time": "2024-12-31",
    })
    assert resp.status_code == 200


def test_daily_empty_result():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_daily_empty_symbols():
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": [],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 422


def test_daily_reversed_dates():
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-31",
        "end_time": "2024-01-01",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_daily_invalid_date_format():
    client = make_test_app()
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2025/07/14",  # 斜杠分隔非 ISO
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 422


def test_daily_sdk_not_ready():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_daily_sdk_query_failed():
    gw = FakeGateway(ready=True, result=None)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "SDK_QUERY_FAILED"


def test_daily_error_has_request_id():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "codes": ["000001.SZ"],
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


def test_minute_success():
    client = make_test_app()
    resp = client.post("/minute", json={
        "codes": ["000001.SZ"], "period": "min5",
        "start_time": "2024-01-02", "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


def test_minute_default_period_min1():
    client = make_test_app()
    resp = client.post("/minute", json={
        "codes": ["000001.SZ"], "start_time": "2024-01-02", "end_time": "2024-01-02",
    })
    assert resp.status_code == 200


def test_minute_invalid_period():
    client = make_test_app()
    resp = client.post("/minute", json={"codes": ["000001.SZ"], "period": "day"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_minute_empty_symbols():
    client = make_test_app()
    resp = client.post("/minute", json={"codes": []})
    assert resp.status_code == 422


def test_realtime_sdk_not_ready_returns_503():
    """SDK 未就绪时 /realtime fallback 也失败，返回 503 SDK_NOT_READY。"""
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.get("/realtime")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_realtime_fallback_empty_when_cache_empty():
    """缓存空 + fallback 也无数据时返回 200 {"data": []}（非 503）。"""
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/realtime")
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_realtime_active_returns_data():
    """订阅激活 + 注入缓存数据后 /realtime 返回数据。"""
    from dataclasses import dataclass
    from datetime import datetime
    from app.http_app import create_app

    @dataclass
    class Snap:
        code: str
        trade_time: datetime
        last: float
        pre_close: float
        open: float
        high: float
        low: float
        volume: int
        amount: float

    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gw)
    app.state.realtime_service.on_snapshot(
        Snap("000001.SZ", datetime(2024, 1, 2, 9, 30), 10.3, 10.2, 10.2, 10.5, 10.1, 1000, 10300.0)
    )
    app.state.realtime_service.set_active(True)
    client = TestClient(app)
    resp = client.get("/realtime")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"
    assert body["data"][0]["last"] == 10.3


def test_realtime_symbols_filter():
    """GET /realtime?codes=000001.SZ 只返回指定 code 的快照；不传返回全市场。"""
    from dataclasses import dataclass
    from datetime import datetime
    from app.http_app import create_app

    @dataclass
    class Snap:
        code: str
        trade_time: datetime
        last: float
        pre_close: float
        open: float
        high: float
        low: float
        volume: int
        amount: float

    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gw)
    app.state.realtime_service.on_snapshot(
        Snap("000001.SZ", datetime(2024, 1, 2, 9, 30), 10.3, 10.2, 10.2, 10.5, 10.1, 1000, 10300.0)
    )
    app.state.realtime_service.on_snapshot(
        Snap("600000.SH", datetime(2024, 1, 2, 9, 30), 20.0, 19.5, 19.5, 20.5, 19.0, 2000, 40000.0)
    )
    app.state.realtime_service.set_active(True)
    client = TestClient(app)
    # 不传 symbols 返回全部
    resp = client.get("/realtime")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 2
    # 传单个 symbols 只返回指定 code
    resp = client.get("/realtime?codes=000001.SZ")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"
    # 传多个 symbols（逗号分隔）
    resp = client.get("/realtime?codes=000001.SZ,600000.SH")
    assert len(resp.json()["data"]) == 2
    # 传不存在的 code 返回空数组
    resp = client.get("/realtime?codes=999999.SZ")
    assert resp.json() == {"data": []}


def _make_realtime_app_with_types():
    """构造带股票+指数+ETF 三类快照缓存的 /realtime 测试应用。

    返回 (client, app) 供调用方进一步断言。set_type_map 注入完整类型映射，
    使 ?types= 过滤可按 security_type 筛选。
    """
    from dataclasses import dataclass
    from datetime import datetime
    from app.http_app import create_app

    @dataclass
    class Snap:
        code: str
        trade_time: datetime
        last: float
        pre_close: float
        open: float
        high: float
        low: float
        volume: int
        amount: float

    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gw)
    rt = app.state.realtime_service
    rt.set_type_map({
        "000001.SZ": "stock",
        "000001.SH": "index",
        "510300.SH": "etf",
    })
    rt.on_snapshot(
        Snap("000001.SZ", datetime(2024, 1, 2, 9, 30), 10.3, 10.2, 10.2, 10.5, 10.1, 1000, 10300.0)
    )
    rt.on_snapshot(
        Snap("000001.SH", datetime(2024, 1, 2, 9, 30), 3200.0, 3150.0, 3155.0, 3210.0, 3148.0, 0, 0.0)
    )
    rt.on_snapshot(
        Snap("510300.SH", datetime(2024, 1, 2, 9, 30), 4.1, 4.0, 4.0, 4.2, 3.9, 500, 2000.0)
    )
    rt.set_active(True)
    return TestClient(app), app


def test_realtime_types_filter_etf():
    """?types=etf 只返回 security_type 为 etf 的快照。"""
    client, _ = _make_realtime_app_with_types()
    resp = client.get("/realtime?types=etf")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "510300.SH"
    assert body["data"][0]["security_type"] == "etf"


def test_realtime_types_filter_multiple():
    """?types=etf,index 返回多类型快照（逗号分隔）。"""
    client, _ = _make_realtime_app_with_types()
    resp = client.get("/realtime?types=etf,index")
    assert resp.status_code == 200
    body = resp.json()
    codes = {row["code"] for row in body["data"]}
    assert codes == {"510300.SH", "000001.SH"}


def test_realtime_types_filter_stock_excludes_others():
    """?types=stock 只返回股票，排除指数和 ETF。"""
    client, _ = _make_realtime_app_with_types()
    resp = client.get("/realtime?types=stock")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"
    assert body["data"][0]["security_type"] == "stock"


def test_realtime_types_invalid_value_returns_422():
    """?types=bond 非法值 → 422 且 error.code == INVALID_REQUEST。"""
    client, _ = _make_realtime_app_with_types()
    resp = client.get("/realtime?types=bond")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_realtime_codes_and_types_combined():
    """codes + types 叠加：先按 codes 过滤再按 types 过滤。"""
    client, _ = _make_realtime_app_with_types()
    # 传股票+ETF 的 codes，但 types=etf → 只剩 ETF
    resp = client.get("/realtime?codes=000001.SZ,510300.SH&types=etf")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "510300.SH"
    assert body["data"][0]["security_type"] == "etf"


def test_realtime_no_types_returns_all():
    """不传 types 时行为不变：返回全部快照（含 security_type 字段）。"""
    client, _ = _make_realtime_app_with_types()
    resp = client.get("/realtime")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 3
    types_set = {row["security_type"] for row in body["data"]}
    assert types_set == {"stock", "index", "etf"}


def test_health_has_realtime_field():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "realtime" in resp.json()


def test_realtime_startup_activates_subscription():
    """startup 事件应触发订阅启动，FakeGateway no-op 不会抛异常，is_active 应为 True。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        # startup 已执行：FakeGateway.start_snapshot_subscription 被调用 + set_active(True)
        assert gw.sub_start_called == 1
        assert app.state.realtime_service.is_active() is True
        # /realtime 应返回 200（缓存为空，因为 FakeGateway 不真正推送数据）
        resp = client.get("/realtime")
        assert resp.status_code == 200
        assert resp.json() == {"data": []}


def test_realtime_not_active_after_startup_failure():
    """startup 中订阅启动抛异常时 is_active=False，但 SDK 就绪，/realtime fallback 返回空（200）。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    # 让 get_realtime_universe 抛异常模拟订阅启动失败
    # （startup 订阅失败；/realtime 走 fallback 但 codes=None 短路返回空，不触发 get_realtime_universe）
    def _boom():
        raise RuntimeError("simulated failure")
    gw.get_realtime_universe = _boom
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        # startup 中 get_realtime_universe 抛异常被 try/except 捕获，is_active 保持 False
        assert app.state.realtime_service.is_active() is False
        # /realtime codes=None：fallback 短路返回空（不调 get_realtime_universe），SDK 就绪 → 200
        resp = client.get("/realtime")
        assert resp.status_code == 200
        assert resp.json() == {"data": []}


def test_realtime_startup_subscribes_combined_list_with_index():
    """startup 事件应触发订阅启动传入合并 code_list（股票+指数+ETF），含各类型代码。

    FakeGateway.start_snapshot_subscription 会记录 code_list 到 _sub_code_list，
    验证该列表同时包含股票代码、指数代码和 ETF 代码。
    """
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        # 订阅已启动
        assert gw.sub_start_called == 1
        # _sub_code_list 应包含股票 + 指数 + ETF 代码
        assert gw._sub_code_list is not None
        assert "000001.SZ" in gw._sub_code_list  # 股票
        assert "600000.SH" in gw._sub_code_list  # 股票
        assert "000001.SH" in gw._sub_code_list  # 指数（上证指数）
        assert "399001.SZ" in gw._sub_code_list  # 指数（深证成指）
        assert "510300.SH" in gw._sub_code_list  # ETF（沪深300ETF）
        assert "159915.SZ" in gw._sub_code_list  # ETF（创业板ETF）
        # /realtime 返回 200
        resp = client.get("/realtime")
        assert resp.status_code == 200


def test_shutdown_calls_gateway_logout():
    """lifespan shutdown 应调用 gateway.logout() 释放 SDK 连接。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        # startup 触发 login
        assert gw.login_called >= 1
    # 退出 with 块后 shutdown 触发 logout
    assert gw.logout_called >= 1


def test_sdk_gate_rejects_when_busy():
    """SdkGate 满载时 try_acquire 返回 False，路由返回 503 SERVICE_BUSY。"""
    from app.http_app import SdkGate
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    app = create_app(config=config, gateway=gw)
    app.state.sdk_gate = SdkGate(max_concurrent=1)
    assert app.state.sdk_gate.try_acquire() is True  # 占用唯一槽位
    client = TestClient(app)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_BUSY"
    app.state.sdk_gate.release()
    resp2 = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp2.status_code == 200


def test_sdk_gate_allows_under_limit():
    """并发数在上限内时正常返回 200。"""
    from app.http_app import SdkGate
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    app = create_app(config=config, gateway=gw)
    app.state.sdk_gate = SdkGate(max_concurrent=5)
    client = TestClient(app)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200


def test_lifespan_skips_subscription_outside_window():
    """非窗口期启动：调度器不启动订阅，sub_start_called 保持 0。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="23:58", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        assert gw.sub_start_called == 0
    assert gw.logout_called >= 1


def test_lifespan_starts_subscription_in_window():
    """窗口期启动：调度器启动订阅，调用 start_snapshot_subscription。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        assert gw.sub_start_called == 1
        assert app.state.realtime_service.is_active() is True


def test_lifespan_starts_watchdog_in_window():
    """窗口期启动订阅后应启动 watchdog 线程。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59",
                    watchdog_interval_sec=999)
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        rt_svc = app.state.realtime_service
        assert rt_svc._watchdog_thread is not None
        assert rt_svc._watchdog_thread.is_alive()


def test_lifespan_skips_subscription_no_calendar():
    """calendar=None（gateway 未 login）时不启动订阅。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=None)
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        assert gw.sub_start_called == 0


def test_health_503_when_stale_in_window():
    """窗口期内订阅 stale → /health 返回 503。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        rt_svc = app.state.realtime_service
        rt_svc.set_active(False)
        rt_svc._deactivation_reason = "stale"
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["realtime_detail"] == "inactive_stale"


def test_health_200_offhours_inactive():
    """非窗口期 inactive → /health 返回 200 + realtime_detail=inactive_offhours。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="23:58", subscription_close="23:59")
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["realtime_detail"] == "inactive_offhours"


def test_shutdown_stops_watchdog():
    """lifespan shutdown 应调用 stop_watchdog。"""
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()},
                     calendar=_today_cal())
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021,
                    subscription_open="00:00", subscription_close="23:59",
                    watchdog_interval_sec=999)
    app = create_app(config=config, gateway=gw)
    with TestClient(app) as client:
        _wait_scheduler(app)
        rt_svc = app.state.realtime_service
        assert rt_svc._watchdog_thread is not None
    assert rt_svc._stop_flag.is_set()
    rt_svc._watchdog_thread.join(timeout=2)
    assert not rt_svc._watchdog_thread.is_alive()

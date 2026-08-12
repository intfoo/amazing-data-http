"""stale 降级：缓存按年龄三态（新鲜/stale/过旧走 fallback）。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from app.realtime_service import RealtimeService
from tests.conftest import FakeGateway


def _make_app():
    config = Config(username="u", password="p", ip="1.2.3.4", port=1,
                    auth_required=False, stale_threshold_sec=90,
                    stale_max_age_sec=300)
    gw = FakeGateway(ready=True)
    app = create_app(config, gw)
    return app


def _seed_cache(app, age_sec: float):
    rt: RealtimeService = app.state.realtime_service
    rt._cache["000001.SZ"] = {"code": "000001.SZ", "last": 10.5,
                              "security_type": "stock"}
    rt._last_snapshot_ts = time.time() - age_sec


class TestRealtimeStale:
    def test_fresh_cache_no_stale_field(self):
        app = _make_app()
        with TestClient(app) as client:
            _seed_cache(app, 10)
            r = client.get("/realtime")
        assert r.status_code == 200
        body = r.json()
        assert body["data"] and "stale" not in body

    def test_stale_cache_marked(self):
        app = _make_app()
        with TestClient(app) as client:
            _seed_cache(app, 120)  # 90 < 120 <= 300
            r = client.get("/realtime")
        body = r.json()
        assert body["stale"] is True
        assert body["cache_age_sec"] >= 120
        assert body["data"]

    def test_too_old_cache_falls_back(self):
        """age > stale_max_age_sec → 放弃缓存走 fallback（无 codes + 有旧缓存语义不变）。"""
        app = _make_app()
        with TestClient(app) as client:
            _seed_cache(app, 400)
            r = client.get("/realtime")
        body = r.json()
        assert "stale" not in body  # 走 fallback（无 codes 返回 [] 或旧 fallback 缓存）

    def test_cache_age_sec_none_when_never_received(self):
        app = _make_app()
        rt: RealtimeService = app.state.realtime_service
        assert rt.cache_age_sec is None

    def test_clear_cache_resets_ts(self):
        app = _make_app()
        rt: RealtimeService = app.state.realtime_service
        rt._last_snapshot_ts = time.time()
        rt.clear_cache()
        assert rt.cache_age_sec is None


class TestHealthDiagnostics:
    def test_health_has_login_diag_fields(self):
        app = _make_app()
        with TestClient(app) as client:
            r = client.get("/health")
        body = r.json()
        assert "last_login_error" in body
        assert "reconnect_attempts" in body
        assert "cache_age_sec" in body

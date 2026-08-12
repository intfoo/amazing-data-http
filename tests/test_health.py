import datetime
from unittest.mock import MagicMock

from app.config import Config
from app.health import HealthService


def _make_config(**kwargs):
    defaults = dict(
        username="u", password="p", ip="1.2.3.4", port=3021,
        subscription_open="00:00", subscription_close="23:59",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _make_gw(ready=True, calendar=None):
    """构造假 gateway。calendar 与 calendar_set 必须同步（HealthService 读 calendar_set）。

    注意：不能裸用 MagicMock 不设 calendar_set——MagicMock 自动属性是 truthy mock，
    会绕过"空日历走 weekday 兜底"的分支语义。
    """
    gw = MagicMock()
    gw.is_ready.return_value = ready
    gw.calendar = calendar
    gw.calendar_set = frozenset(calendar) if calendar else frozenset()
    return gw


def _make_realtime_svc(active=False, reason=None, last_ts=0.0):
    svc = MagicMock()
    svc.is_active.return_value = active
    svc.deactivation_reason.return_value = reason
    svc.last_snapshot_ts.return_value = last_ts
    return svc


def test_realtime_detail_active():
    gw = _make_gw(calendar=[20240102])
    rt = _make_realtime_svc(active=True)
    hs = HealthService(_make_config(), gw, rt)
    assert hs._realtime_detail() == "active"


def test_realtime_detail_inactive_offhours():
    """非窗口期（calendar 不含今天 + 严格日历模式）→ inactive_offhours。"""
    gw = _make_gw(calendar=[20231231])  # not today
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(calendar_fallback_weekday=False), gw, rt)
    assert hs._realtime_detail() == "inactive_offhours"


def test_realtime_detail_inactive_stale():
    """窗口期内 inactive + reason=stale → inactive_stale。"""
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw = _make_gw(calendar=[today])
    rt = _make_realtime_svc(active=False, reason="stale", last_ts=1000.0)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs._realtime_detail() == "inactive_stale"


def test_realtime_detail_inactive_error():
    """窗口期内 inactive + reason=error → inactive_error。"""
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw = _make_gw(calendar=[today])
    rt = _make_realtime_svc(active=False, reason="error")
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs._realtime_detail() == "inactive_error"


def test_realtime_detail_inactive_not_started():
    """窗口期内 inactive + 无 reason + 无数据 → inactive_not_started。"""
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw = _make_gw(calendar=[today])
    rt = _make_realtime_svc(active=False, reason=None, last_ts=0.0)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs._realtime_detail() == "inactive_not_started"


def test_is_ok_offhours_inactive_returns_true():
    """非窗口期 inactive → is_ok() True（不要求 realtime 活跃）。"""
    gw = _make_gw(calendar=[20231231])
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(calendar_fallback_weekday=False), gw, rt)
    assert hs.is_ok() is True


def test_is_ok_window_active_returns_true():
    """窗口期内 active → is_ok() True。"""
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw = _make_gw(calendar=[today])
    rt = _make_realtime_svc(active=True)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs.is_ok() is True


def test_is_ok_window_inactive_returns_false():
    """窗口期内 inactive → is_ok() False（触发 503）。"""
    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    gw = _make_gw(calendar=[today])
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(subscription_open="00:00", subscription_close="23:59"), gw, rt)
    assert hs.is_ok() is False


def test_is_ok_calendar_none_returns_true():
    """calendar=None → 非窗口 → is_ok() True。"""
    gw = _make_gw(calendar=None)
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(), gw, rt)
    assert hs.is_ok() is True


def test_is_ok_gateway_not_ready_returns_false():
    gw = _make_gw(ready=False, calendar=None)
    rt = _make_realtime_svc(active=True)
    hs = HealthService(_make_config(), gw, rt)
    assert hs.is_ok() is False


def test_status_includes_realtime_detail():
    """status() 返回 dict 应包含 realtime_detail 字段。"""
    gw = _make_gw(calendar=[20231231])
    rt = _make_realtime_svc(active=False)
    hs = HealthService(_make_config(calendar_fallback_weekday=False), gw, rt)
    status = hs.status()
    assert "realtime_detail" in status
    assert status["realtime_detail"] == "inactive_offhours"


def test_status_no_realtime_svc():
    """realtime_service=None 时 realtime_detail=unavailable。"""
    gw = _make_gw(calendar=None)
    hs = HealthService(_make_config(), gw, None)
    status = hs.status()
    assert status["realtime_detail"] == "unavailable"

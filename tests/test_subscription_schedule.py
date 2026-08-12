import datetime

from app.subscription_schedule import is_subscription_window, parse_hhmm


def test_parse_hhmm_normal():
    assert parse_hhmm("09:00") == datetime.time(9, 0)
    assert parse_hhmm("15:20") == datetime.time(15, 20)
    assert parse_hhmm("00:00") == datetime.time(0, 0)
    assert parse_hhmm("23:59") == datetime.time(23, 59)


def test_is_window_trading_day_within_window():
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, cal) is True


def test_is_window_trading_day_before_window():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 8, 59)
    assert is_subscription_window(now, cal) is False


def test_is_window_trading_day_after_window():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 15, 21)
    assert is_subscription_window(now, cal) is False


def test_is_window_boundary_open():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 9, 0)
    assert is_subscription_window(now, cal) is True


def test_is_window_boundary_close():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 15, 20)
    assert is_subscription_window(now, cal) is True


def test_is_window_non_trading_day():
    """日历不含今天 + calendar_fallback_weekday=False → False（严格按日历）。"""
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 4, 10, 30)  # not in calendar
    assert is_subscription_window(now, cal, calendar_fallback_weekday=False) is False


def test_is_window_calendar_fallback_weekday_true():
    """日历不含今天但工作日 + fallback=True → True（weekday 兜底）。"""
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 4, 10, 30)  # Thursday, not in calendar
    assert is_subscription_window(now, cal, calendar_fallback_weekday=True) is True


def test_is_window_calendar_fallback_weekday_weekend():
    """日历不含今天且周末 + fallback=True → False。"""
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 6, 10, 30)  # Saturday, not in calendar
    assert is_subscription_window(now, cal, calendar_fallback_weekday=True) is False


def test_is_window_calendar_fallback_default_true():
    """默认 calendar_fallback_weekday=True（不传参数）。"""
    cal = [20240102, 20240103]
    now = datetime.datetime(2024, 1, 4, 10, 30)  # Thursday, not in calendar
    assert is_subscription_window(now, cal) is True


def test_is_window_calendar_none_weekday_fallback():
    """calendar=None + 工作日窗口内 → True（weekday 兜底，默认）。"""
    now = datetime.datetime(2024, 1, 2, 10, 30)  # 周二
    assert is_subscription_window(now, None) is True


def test_is_window_calendar_none_strict_mode():
    """calendar=None + calendar_fallback_weekday=False → False（严格模式）。"""
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, None, calendar_fallback_weekday=False) is False


def test_is_window_calendar_empty_weekday_fallback():
    """calendar=[] + 工作日窗口内 → True（空列表同样走 weekday 兜底）。"""
    now = datetime.datetime(2024, 1, 2, 10, 30)  # 周二
    assert is_subscription_window(now, []) is True


def test_is_window_custom_times():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 8, 30)
    assert is_subscription_window(now, cal, open_time="08:00", close_time="16:00") is True
    assert is_subscription_window(now, cal) is False  # default 09:00


def test_is_window_wide_window_always_true_on_trading_day():
    cal = [20240102]
    now = datetime.datetime(2024, 1, 2, 0, 0)
    assert is_subscription_window(now, cal, open_time="00:00", close_time="23:59") is True
    now = datetime.datetime(2024, 1, 2, 23, 58)
    assert is_subscription_window(now, cal, open_time="00:00", close_time="23:59") is True


# ---------------------------------------------------------------------------
# calendar=None / 空 → weekday 兜底（2026-08-12 事故修复）
# ---------------------------------------------------------------------------

class TestNoneCalendarFallback:
    """calendar=None（登录前/重连失败）时 weekday 兜底，避免盘中误判"不在窗口"。"""

    def test_none_calendar_weekday_in_window(self):
        """calendar=None + 周三 14:00 → True（weekday 兜底）。"""
        wed = datetime.datetime(2026, 8, 12, 14, 0)  # 周三
        assert is_subscription_window(wed, None) is True

    def test_none_calendar_weekend(self):
        sat = datetime.datetime(2026, 8, 15, 14, 0)  # 周六
        assert is_subscription_window(sat, None) is False

    def test_none_calendar_out_of_hours(self):
        wed_evening = datetime.datetime(2026, 8, 12, 16, 0)  # 周三出窗
        assert is_subscription_window(wed_evening, None) is False

    def test_none_calendar_strict_mode(self):
        """calendar_fallback_weekday=False + calendar=None → False（严格模式）。"""
        wed = datetime.datetime(2026, 8, 12, 14, 0)
        assert is_subscription_window(wed, None, calendar_fallback_weekday=False) is False

    def test_cross_day_stale_calendar(self):
        """跨日残留 calendar（不含今天）+ 工作日 → weekday 兜底 True。"""
        wed = datetime.datetime(2026, 8, 12, 14, 0)
        assert is_subscription_window(wed, [20260811]) is True


def test_is_subscription_window_accepts_aware_datetime_and_set():
    """tz-aware now + frozenset calendar 与 naive+list 行为一致。"""
    from zoneinfo import ZoneInfo
    from app.subscription_schedule import is_subscription_window
    cal_list = [20260812]  # 2026-08-12 周三
    cal_set = frozenset(cal_list)
    naive = datetime.datetime(2026, 8, 12, 10, 0)
    aware = datetime.datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert is_subscription_window(naive, cal_list) == is_subscription_window(aware, cal_set)
    assert is_subscription_window(aware, cal_set) is True
    outside = datetime.datetime(2026, 8, 12, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert is_subscription_window(outside, cal_set) is False

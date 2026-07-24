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


def test_is_window_calendar_none():
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, None) is False


def test_is_window_calendar_empty():
    now = datetime.datetime(2024, 1, 2, 10, 30)
    assert is_subscription_window(now, []) is False


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

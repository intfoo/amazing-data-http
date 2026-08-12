"""调度器启动订阅前应热刷新交易日历（防 SDK 日历快照过期导致当日数据静默为空）。"""

from app.config import Config
from app.realtime_service import RealtimeService
from app.subscription_scheduler import SubscriptionScheduler


class FakeGW:
    def __init__(self):
        self._calendar = [20240101]
        self.refresh_called = 0

    @property
    def calendar(self):
        return self._calendar

    def refresh_calendar(self):
        self.refresh_called += 1
        self._calendar = [20240101, 20240102]
        return self._calendar

    def stop_subscription(self):
        pass

    def get_realtime_universe(self):
        return {"000001.SZ": "stock"}

    def start_snapshot_subscription(self, code_list, on_data, on_error=None):
        pass


def make_config():
    return Config(username="u", password="p", ip="1.2.3.4", port=3021)


def test_start_subscription_refreshes_calendar():
    gw = FakeGW()
    rt = RealtimeService(gateway=None)
    sched = SubscriptionScheduler(gw, rt, make_config())
    sched._start_subscription(gw.calendar)
    try:
        assert gw.refresh_called == 1
        assert rt.is_active() is True
    finally:
        rt.stop_watchdog()

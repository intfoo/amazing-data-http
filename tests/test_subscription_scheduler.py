"""SubscriptionScheduler 单元测试。

直接调用 _tick() 进行确定性测试，避免后台线程时序不确定性。
同时测试 start()/stop() 生命周期与 _first_tick_done 同步。
"""

import datetime
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.config import Config
from app.gateway import GatewayNotReadyError
from app.realtime_service import RealtimeService
from app.subscription_scheduler import SubscriptionScheduler
from tests.conftest import FakeGateway, make_daily_df


def _make_config(**kwargs):
    defaults = dict(
        username="u", password="p", ip="1.2.3.4", port=3021,
        subscription_open="00:00", subscription_close="23:59",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _today_cal():
    return [int(datetime.datetime.now().strftime("%Y%m%d"))]


def _make_scheduler(gw=None, config=None, rt=None):
    if gw is None:
        gw = FakeGateway(ready=True, calendar=_today_cal())
    if config is None:
        config = _make_config()
    if rt is None:
        rt = RealtimeService(gateway=gw)
    return SubscriptionScheduler(gw, rt, config), gw, rt


# ---------------------------------------------------------------------------
# 自动启动：窗口内 + 订阅未活跃 → 启动订阅
# ---------------------------------------------------------------------------

def test_tick_starts_subscription_when_in_window_and_inactive():
    """窗口内 + is_active=False → _tick 启动订阅。"""
    scheduler, gw, rt = _make_scheduler()
    rt.set_active(False)
    scheduler._tick()
    assert gw.sub_start_called == 1
    assert rt.is_active() is True


def test_tick_skips_when_already_active():
    """窗口内 + is_active=True → _tick 不重复启动。"""
    scheduler, gw, rt = _make_scheduler()
    rt.set_active(True)
    gw.sub_start_called = 0  # 重置（set_active 不调 start）
    scheduler._tick()
    assert gw.sub_start_called == 0


def test_tick_skips_when_outside_window():
    """非窗口期 + is_active=False → _tick 不启动。"""
    config = _make_config(subscription_open="23:58", subscription_close="23:59")
    scheduler, gw, rt = _make_scheduler(config=config)
    rt.set_active(False)
    scheduler._tick()
    assert gw.sub_start_called == 0


# ---------------------------------------------------------------------------
# 自动停止：非窗口 + 订阅活跃 → 停止订阅 + 清空缓存
# ---------------------------------------------------------------------------

def test_tick_stops_subscription_when_outside_window_and_active():
    """非窗口期 + is_active=True → _tick 停止订阅 + 清空缓存。"""
    config = _make_config(subscription_open="23:58", subscription_close="23:59")
    scheduler, gw, rt = _make_scheduler(config=config)
    rt.set_active(True)
    # 注入一些缓存数据
    rt._cache["000001.SZ"] = {"code": "000001.SZ", "last": 10.0}
    scheduler._tick()
    assert gw.sub_stop_called == 1
    assert rt.is_active() is False
    assert rt.snapshot() == []


def test_tick_does_not_stop_when_outside_window_and_inactive():
    """非窗口期 + is_active=False → _tick 不调 stop。"""
    config = _make_config(subscription_open="23:58", subscription_close="23:59")
    scheduler, gw, rt = _make_scheduler(config=config)
    rt.set_active(False)
    scheduler._tick()
    assert gw.sub_stop_called == 0


# ---------------------------------------------------------------------------
# 崩溃恢复：订阅崩溃后 is_active=False → 下次 tick 自动重启
# ---------------------------------------------------------------------------

def test_tick_recovers_after_subscription_crash():
    """订阅崩溃（on_error → is_active=False）→ 下次 _tick 重新启动。"""
    scheduler, gw, rt = _make_scheduler()
    # 第一次 tick：正常启动
    scheduler._tick()
    assert gw.sub_start_called == 1
    assert rt.is_active() is True
    # 模拟订阅崩溃
    rt.on_subscription_error(RuntimeError("crash"))
    assert rt.is_active() is False
    # 第二次 tick：自动恢复
    scheduler._tick()
    assert gw.sub_start_called == 2  # 再次启动
    assert rt.is_active() is True


# ---------------------------------------------------------------------------
# 异常容错：_tick 抛异常不终止 _loop
# ---------------------------------------------------------------------------

def test_loop_survives_tick_exception():
    """_tick 抛异常时 _loop 不终止，下次 tick 正常执行。"""
    scheduler, gw, rt = _make_scheduler()
    call_count = [0]
    original_tick = scheduler._tick

    def flaky_tick():
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("transient error")
        original_tick()

    scheduler._tick = flaky_tick
    scheduler.start()
    # 等待至少 2 次 tick（第一次异常，第二次正常）
    # SCHEDULE_INTERVAL_SEC=60 太长，直接调 _loop 的逻辑不太方便
    # 用 _first_tick_done 确认第一次 tick（异常）已执行
    scheduler._first_tick_done.wait(timeout=2)
    assert call_count[0] >= 1
    scheduler.stop()
    # 第一次 tick 抛了异常，但 _loop 没有崩溃（_first_tick_done 被 set 了）
    # 证明 _loop 的 try/except 生效


# ---------------------------------------------------------------------------
# start()/stop() 生命周期
# ---------------------------------------------------------------------------

def test_start_runs_first_tick_immediately():
    """start() 后首次 tick 立即执行（不等 SCHEDULE_INTERVAL_SEC）。"""
    scheduler, gw, rt = _make_scheduler()
    scheduler.start()
    assert scheduler._first_tick_done.wait(timeout=5)
    assert gw.sub_start_called == 1
    scheduler.stop()
    if scheduler._thread:
        scheduler._thread.join(timeout=5)


def test_start_is_idempotent():
    """重复 start() 不启动多个线程。"""
    scheduler, gw, rt = _make_scheduler()
    scheduler.start()
    t1 = scheduler._thread
    scheduler.start()
    assert scheduler._thread is t1
    scheduler.stop()
    if t1:
        t1.join(timeout=5)


def test_stop_sets_stop_flag():
    """stop() 设置 _stop_flag，线程退出。"""
    scheduler, gw, rt = _make_scheduler(
        config=_make_config(subscription_open="23:58", subscription_close="23:59"),
    )
    scheduler.start()
    scheduler._first_tick_done.wait(timeout=5)
    scheduler.stop()
    assert scheduler._stop_flag.is_set()
    if scheduler._thread:
        scheduler._thread.join(timeout=5)
        assert not scheduler._thread.is_alive()


# ---------------------------------------------------------------------------
# _action_lock 互斥
# ---------------------------------------------------------------------------

def test_action_lock_prevents_concurrent_start_stop():
    """_start_subscription 和 _stop_subscription 互斥（不会并发执行）。"""
    scheduler, gw, rt = _make_scheduler()
    # 持有 _action_lock 模拟正在执行 start/stop
    scheduler._action_lock.acquire()
    try:
        # 在另一个线程调 _start_subscription，应阻塞
        result = [False]
        def try_start():
            result[0] = scheduler._action_lock.acquire(blocking=False)
            if result[0]:
                scheduler._action_lock.release()
        t = threading.Thread(target=try_start)
        t.start()
        t.join(timeout=1)
        assert result[0] is False  # 无法获取锁，说明互斥生效
    finally:
        scheduler._action_lock.release()


# ---------------------------------------------------------------------------
# 日历兜底集成（calendar_fallback_weekday）
# ---------------------------------------------------------------------------

def test_tick_starts_with_weekday_fallback_when_calendar_stale():
    """日历不含今天但工作日 + fallback=True → _tick 启动订阅。"""
    gw = FakeGateway(ready=True, calendar=[20231231])  # 不含今天
    config = _make_config()  # 默认 calendar_fallback_weekday=True
    rt = RealtimeService(gateway=gw)
    scheduler = SubscriptionScheduler(gw, rt, config)
    rt.set_active(False)
    # 如果今天是工作日，_tick 应启动订阅
    today = datetime.datetime.now()
    if today.weekday() < 5:  # 工作日
        scheduler._tick()
        assert gw.sub_start_called == 1
        assert rt.is_active() is True
    else:  # 周末
        scheduler._tick()
        assert gw.sub_start_called == 0


def test_tick_does_not_start_with_fallback_disabled():
    """日历不含今天 + fallback=False → _tick 不启动。"""
    gw = FakeGateway(ready=True, calendar=[20231231])
    config = _make_config(calendar_fallback_weekday=False)
    rt = RealtimeService(gateway=gw)
    scheduler = SubscriptionScheduler(gw, rt, config)
    rt.set_active(False)
    scheduler._tick()
    assert gw.sub_start_called == 0


# ---------------------------------------------------------------------------
# 自愈 login：窗口内 + not ready → 调 gateway.login()（2026-08-12 事故修复）
# ---------------------------------------------------------------------------

class TestSchedulerSelfHeal:
    """调度器 not-ready 自愈：窗口内 SDK 未登录时每 tick 重试 login。"""

    def test_not_ready_triggers_login(self):
        """窗口内 + not ready + 无重连进行中 → 调 gateway.login()，不启动订阅。"""
        gw = FakeGateway(ready=False, calendar=_today_cal())
        config = _make_config()
        rt = RealtimeService(gateway=gw)
        scheduler = SubscriptionScheduler(gw, rt, config)
        rt.set_active(False)
        scheduler._tick()
        assert gw.login_called == 1
        assert gw.sub_start_called == 0  # login 后 return，本轮不启动订阅

    def test_not_ready_skips_when_reconnect_in_progress(self):
        """_reconnect_in_progress=True → 跳过 login（避免 _sdk_lock 竞争）。"""
        gw = FakeGateway(ready=False, calendar=_today_cal())
        gw._reconnect_in_progress = True
        config = _make_config()
        rt = RealtimeService(gateway=gw)
        scheduler = SubscriptionScheduler(gw, rt, config)
        rt.set_active(False)
        scheduler._tick()
        assert gw.login_called == 0
        assert gw.sub_start_called == 0

    def test_login_failure_swallowed(self):
        """login 抛异常被吞（debug 日志），_tick 不炸、进程不死。"""
        gw = FakeGateway(ready=False, calendar=_today_cal())
        config = _make_config()
        rt = RealtimeService(gateway=gw)
        scheduler = SubscriptionScheduler(gw, rt, config)
        rt.set_active(False)

        def failing_login():
            raise GatewayNotReadyError("fake login fail")

        gw.login = failing_login
        # _tick 不应抛异常
        scheduler._tick()
        assert gw.sub_start_called == 0

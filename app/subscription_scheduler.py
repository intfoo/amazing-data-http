"""订阅调度器：后台线程按交易窗口自动启动/停止快照订阅。

解决问题：
1. 服务在非交易时段启动时，lifespan 只检查一次窗口就跳过订阅，
   进入交易时段后无自动启动机制 → 订阅永不启动。
2. 订阅线程崩溃/失活后（on_error / watchdog stale），无自动恢复机制。
3. SDK 交易日历可能不含今天（数据延迟），is_subscription_window 恒 False
   导致订阅永不启动（由 calendar_fallback_weekday 兜底解决）。

调度器每 SCHEDULE_INTERVAL_SEC（默认 60s）检查一次：
- 在窗口内且订阅未活跃 → 启动订阅（先 stop 清理旧资源，再 start）
- 不在窗口内且订阅活跃 → 停止订阅 + 清空缓存
"""

from __future__ import annotations

import datetime
import logging
import threading
import time

from app.config import Config
from app.gateway import Gateway
from app.realtime_service import RealtimeService
from app.subscription_schedule import is_subscription_window

logger = logging.getLogger("amazingdata.scheduler")

SCHEDULE_INTERVAL_SEC = 60  # 调度器检查间隔（秒）


class SubscriptionScheduler:
    """后台调度线程：按交易窗口自动启动/停止快照订阅。"""

    def __init__(
        self,
        gateway: Gateway,
        realtime_service: RealtimeService,
        config: Config,
    ):
        self._gw = gateway
        self._rt = realtime_service
        self._config = config
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._first_tick_done = threading.Event()
        # 防止 start_subscription 与 stop_subscription 并发
        self._action_lock = threading.Lock()

    def start(self) -> None:
        """启动调度线程。幂等（已启动则跳过）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sub-scheduler",
        )
        self._thread.start()
        logger.info("订阅调度器已启动 (间隔=%ds)", SCHEDULE_INTERVAL_SEC)

    def stop(self) -> None:
        """通知调度线程停止。不等待（daemon 线程随进程退出）。"""
        self._stop_flag.set()

    def _loop(self) -> None:
        """调度主循环。"""
        while not self._stop_flag.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("订阅调度器 tick 异常: %s: %s", type(e).__name__, e)
            finally:
                self._first_tick_done.set()
            if self._stop_flag.wait(timeout=SCHEDULE_INTERVAL_SEC):
                break

    def _tick(self) -> None:
        """单次调度检查。"""
        now = datetime.datetime.now()
        cal = self._gw.calendar
        in_window = is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
            calendar_fallback_weekday=self._config.calendar_fallback_weekday,
        )
        if in_window:
            if not self._rt.is_active():
                logger.info("调度器：在订阅窗口内但订阅未活跃，尝试启动")
                self._start_subscription(cal)
        else:
            if self._rt.is_active():
                logger.info("调度器：不在订阅窗口，停止订阅")
                self._stop_subscription()

    def _start_subscription(self, cal: list[int]) -> None:
        """启动快照订阅（先 stop 清理旧资源，再 start）。"""
        with self._action_lock:
            # 交易日历热刷新：SDK 日历是 login 时快照，跨天后不含今天，
            # 会导致当日 K 线查询静默返回空。每天窗口开启启动订阅时顺带刷新。
            try:
                refreshed = self._gw.refresh_calendar()
                if refreshed:
                    cal = refreshed
            except Exception as e:
                logger.warning("调度器刷新交易日历失败（沿用旧日历）: %s: %s", type(e).__name__, e)
            # 先清理旧的订阅资源（防止重复订阅/线程泄漏）
            try:
                self._gw.stop_subscription()
            except Exception as e:
                logger.warning("调度器 stop 旧订阅异常（已忽略）: %s: %s", type(e).__name__, e)
            t0 = time.monotonic()
            try:
                universe = self._gw.get_realtime_universe()
                code_list = list(universe)
                t1 = time.monotonic()
                self._gw.start_snapshot_subscription(
                    code_list,
                    on_data=self._rt.on_snapshot,
                    on_error=self._rt.on_subscription_error,
                )
                self._rt.set_type_map(universe)
                self._rt.set_active(True)
                self._rt.start_watchdog(
                    cal,
                    stale_threshold_sec=self._config.stale_threshold_sec,
                    watchdog_interval_sec=self._config.watchdog_interval_sec,
                    open_time=self._config.subscription_open,
                    close_time=self._config.subscription_close,
                    calendar_fallback_weekday=self._config.calendar_fallback_weekday,
                )
                t2 = time.monotonic()
                logger.info(
                    "调度器启动订阅成功: %d 只 "
                    "(get_realtime_universe=%.3fs subscribe=%.3fs)",
                    len(code_list), t1 - t0, t2 - t1,
                )
            except Exception as e:
                logger.error("调度器启动订阅失败: %s: %s", type(e).__name__, e)
                self._rt.set_active(False)

    def _stop_subscription(self) -> None:
        """停止快照订阅 + 清空缓存。"""
        with self._action_lock:
            try:
                self._gw.stop_subscription()
            except Exception as e:
                logger.warning("调度器 stop 订阅异常（已忽略）: %s: %s", type(e).__name__, e)
            self._rt.stop_watchdog()
            self._rt.set_active(False)
            self._rt.clear_cache()
            logger.info("调度器已停止订阅并清空缓存")

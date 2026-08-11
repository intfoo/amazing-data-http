"""RealtimeService：SDK SubscribeData 订阅回调 → 内存缓存 → GET /realtime 读缓存。

职责边界：
- on_snapshot 回调把 Snapshot 对象转为 JSON 安全 dict，按 code 覆盖写入 _cache
- snapshot 返回全市场快照列表（浅拷贝）
- 不做字段重命名/换算/衍生计算（与 /daily 哲学一致），由主项目 YAML field_map 处理

缓存语义：dict[code, dict] 覆盖写入，每个 code 只保留最新快照，无需淘汰策略。
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.subscription_schedule import is_subscription_window

from app.gateway import GatewayNotReadyError
from app.serializer import serialize_dataframe, serialize_value

logger = logging.getLogger("amazingdata.realtime")

FALLBACK_TTL = 120  # 秒，fallback query_snapshot 缓存有效期（SDK 往返较慢，延长 TTL 减少重查）

# fallback query_snapshot 时间窗口：收盘集合竞价附近，避免拉全量逐笔（5000+行/只 → 几十行）。
# A股15:00收盘，取14:59:00-15:01:00区间，tail(1)取最后一笔（收盘快照）。
# 时分秒毫秒时间戳格式：9点整=90000000，17点25分=172500000（见SDK文档§3.5.4.1）
# 盘中订阅正常时走缓存不走 fallback；此窗口主要服务收盘后/订阅未就绪场景。
# ⚠️ SDK 盘后查询传入盘中窄窗口的实际返回行为未经 probe 验证，生产部署后需确认
#    query_snapshot 返回的是该时段历史快照（预期）而非空。若返回空需调整窗口或方案。
FALLBACK_BEGIN_TIME = 145900000  # 14:59:00.000
FALLBACK_END_TIME = 150100000    # 15:01:00.000


class RealtimeService:
    def __init__(self, gateway):
        self._gw = gateway
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._active = False
        self._fallback_cache: list[dict] | None = None  # query_snapshot fallback 缓存
        self._fallback_time: float = 0
        # singleflight 锁：保证同一时刻只有一个 fallback query_snapshot 在跑。
        # 全市场 query_snapshot 可能数分钟，并发请求若各自发起查询会堆积抢 gateway._lock，
        # 导致整个服务雪崩。改为：第一个请求查，期间其他请求返回旧缓存/空，不阻塞。
        self._fallback_lock = threading.Lock()
        # 按类型缓存快照提取函数。实际场景中 Snapshot 类型固定（股票/指数 2 种），
        # 缓存条目数有界，无需淘汰策略。若未来 SDK 引入更多类型可考虑加上限。
        self._extract_fns: dict = {}

        # Watchdog 相关字段
        self._last_snapshot_ts: float = 0.0
        self._deactivation_reason: str | None = None  # "stale" / "error" / None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_start_ts: float = 0.0
        self._stop_flag = threading.Event()
        # 订阅窗口参数（calendar, open_time, close_time, fallback），
        # 由 set_window_params/start_watchdog 设置，供 on_snapshot 判断窗口外残留帧不复活。
        self._window_params: tuple | None = None

    def on_snapshot(self, data) -> None:
        """订阅回调：Snapshot → dict → 缓存覆盖。异常吞掉，不影响订阅线程。"""
        try:
            record = self._snapshot_to_dict(data)
            code = record.get("code") if record else None
            if code:
                with self._lock:
                    self._cache[code] = record
            self._last_snapshot_ts = time.time()
            # 自动恢复：仅窗口内恢复（盘中抖动场景）。窗口外收到的帧是退订残留帧
            # （stop_subscription 后 SDK daemon 线程未退出），复活会与调度器形成
            # 每分钟 stop→复活→stop 死循环。
            if not self._active:
                if self._in_recovery_window():
                    self._active = True
                    self._deactivation_reason = None
                    logger.info("订阅已恢复：收到数据，重新激活")
                else:
                    logger.debug("非窗口期收到数据帧，忽略自动恢复（退订残留帧）")
        except Exception as e:
            logger.warning("快照转换失败: %s: %s", type(e).__name__, e)

    def on_subscription_error(self, err=None) -> None:
        """订阅线程崩溃/异常退出回调：标记不活跃，/realtime 将返回 503。"""
        self._active = False
        self._deactivation_reason = "error"
        logger.error("实时订阅因错误停用: %s", err)

    def snapshot(self, codes: list[str] | None = None) -> list[dict]:
        """GET /realtime 读缓存，返回快照列表。

        codes 为 None 时返回全市场快照；非空时只返回指定 code 的快照。
        锁内只取 values 引用快照（list 浅复制引用），浅拷贝 dict 在锁外完成，
        避免长时间持锁阻塞 on_snapshot 写入。
        """
        with self._lock:
            if codes is None:
                items = list(self._cache.values())
            else:
                wanted = set(codes)
                items = [v for code, v in self._cache.items() if code in wanted]
        return [dict(v) for v in items]

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._deactivation_reason = None

    def set_window_params(
        self,
        calendar: list[int] | None,
        open_time: str = "09:00",
        close_time: str = "15:20",
        calendar_fallback_weekday: bool = True,
    ) -> None:
        """设置订阅窗口参数，供 on_snapshot 判断窗口外残留帧不自动复活。"""
        self._window_params = (calendar, open_time, close_time, calendar_fallback_weekday)

    def _in_recovery_window(self) -> bool:
        """on_snapshot 自动复活前检查当前是否在订阅窗口内。
        未设置窗口参数（订阅从未正式启动）时保持旧行为（允许恢复）。
        """
        if self._window_params is None:
            return True
        calendar, open_time, close_time, fallback = self._window_params
        return is_subscription_window(
            datetime.datetime.now(), calendar, open_time, close_time, fallback,
        )

    def clear_cache(self) -> None:
        """清空订阅缓存（调度器停止订阅时调用）。"""
        with self._lock:
            self._cache.clear()

    def deactivation_reason(self) -> str | None:
        """供 HealthService 区分 inactive_stale / inactive_not_started / inactive_error。"""
        return self._deactivation_reason

    def last_snapshot_ts(self) -> float:
        """最后一次收到快照数据的时间戳（0=从未收到）。"""
        return self._last_snapshot_ts

    def start_watchdog(
        self,
        calendar: list[int],
        stale_threshold_sec: int = 90,
        watchdog_interval_sec: int = 60,
        open_time: str = "09:00",
        close_time: str = "15:20",
        calendar_fallback_weekday: bool = True,
    ) -> None:
        """启动后台 watchdog 线程。lifespan 订阅启动后调用。"""
        self.set_window_params(calendar, open_time, close_time, calendar_fallback_weekday)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._stop_flag.clear()
        self._watchdog_start_ts = time.time()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            args=(calendar, stale_threshold_sec, watchdog_interval_sec,
                  open_time, close_time, calendar_fallback_weekday),
            daemon=True, name="sub-watchdog",
        )
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        """优雅停止 watchdog。lifespan shutdown 时调用。"""
        self._stop_flag.set()

    def _watchdog_loop(
        self,
        calendar: list[int] | None,
        stale_threshold_sec: int,
        watchdog_interval_sec: int,
        open_time: str,
        close_time: str,
        calendar_fallback_weekday: bool = True,
    ) -> None:
        """watchdog 主循环：每 watchdog_interval_sec 检查一次订阅存活状态。"""
        while self._active and not self._stop_flag.is_set():
            if self._stop_flag.wait(timeout=watchdog_interval_sec):
                break
            if not self._active:
                break
            now = datetime.datetime.now()
            if not is_subscription_window(
                now, calendar, open_time, close_time, calendar_fallback_weekday,
            ):
                continue
            if self._last_snapshot_ts == 0:
                # 从未收到数据：检查启动后是否超过阈值
                elapsed_since_start = time.time() - self._watchdog_start_ts
                if elapsed_since_start > stale_threshold_sec:
                    logger.error(
                        "订阅启动后 %.0fs 未收到数据，标记失活",
                        elapsed_since_start,
                    )
                    self._active = False
                    self._deactivation_reason = "stale"
                    break
                continue
            elapsed = time.time() - self._last_snapshot_ts
            if elapsed > stale_threshold_sec:
                logger.error(
                    "订阅失活：盘中 %.0fs 无数据",
                    elapsed,
                )
                self._active = False
                self._deactivation_reason = "stale"
                break

    def fallback_snapshot(self, codes: list[str] | None = None) -> list[dict]:
        """订阅缓存为空时的 fallback：用 query_snapshot 查当日历史快照。

        取每只股票的最后一行（最新/收盘快照）序列化返回。
        结果带 FALLBACK_TTL 秒缓存，避免每次请求都查 SDK。
        codes 过滤在缓存结果上应用。查询失败返回空列表（不抛异常，让 /realtime 返回空）。

        ⚠️ 无 codes 时不触发全市场 query_snapshot：全市场查询耗时数分钟，持 gateway._lock
        期间会阻塞 /daily、/minute 等所有 SDK 查询导致服务雪崩。订阅无推送时返回空
        （或旧缓存），符合设计文档 §2.2 "订阅刚启动未收到数据 → 200 {"data": []}" 约定。
        仅当客户端显式传 codes 时才查指定股票（少量 codes，秒级返回）。

        singleflight：codes 非空时，同一时刻只允许一个 query_snapshot 在跑，期间其他
        请求返回旧缓存/空，不重复查询、不堆积抢 gateway._lock。
        """
        if not codes:
            # 无 codes：不查全市场。有旧缓存返回缓存；缓存空时检查 SDK 就绪
            # （未就绪抛 GatewayNotReadyError 让路由转 503，保持错误语义不变）
            if self._fallback_cache:
                return list(self._fallback_cache)
            if not self._gw.is_ready():
                raise GatewayNotReadyError("gateway not ready")
            return []
        now = time.time()
        # 1. 缓存有效 → 直接返回过滤结果
        if self._fallback_cache is not None and now - self._fallback_time <= FALLBACK_TTL:
            return self._filter_fallback(codes)
        # 2. 缓存过期/空 → 非阻塞抢 singleflight 锁
        if not self._fallback_lock.acquire(blocking=False):
            # 已有查询在跑：返回旧缓存（哪怕过期）或空，不阻塞、不重复查询
            if self._fallback_cache:
                logger.info("fallback 查询进行中，返回旧缓存: %d 条",
                            len(self._fallback_cache))
                return self._filter_fallback(codes)
            logger.info("fallback 查询进行中，缓存为空，返回 []")
            return []
        try:
            # 双检：抢锁期间可能已被其他请求填充缓存
            now = time.time()
            if self._fallback_cache is not None and now - self._fallback_time <= FALLBACK_TTL:
                return self._filter_fallback(codes)
            # codes 此处必非空（无 codes 已在方法开头短路返回），直接用作查询列表
            try:
                result = self._gw.query_snapshot(
                    codes,
                    begin_time=FALLBACK_BEGIN_TIME, end_time=FALLBACK_END_TIME,
                )
            except GatewayNotReadyError:
                raise
            except Exception as e:
                logger.warning("fallback query_snapshot 失败: %s: %s", type(e).__name__, e)
                return self._filter_fallback(codes) if self._fallback_cache else []
            # 合并每只股票的最后一行（最新快照），一次 serialize_dataframe 序列化，
            # 避免几千只股票逐只调 serialize_dataframe 的开销。
            import pandas as pd
            tails = [df.tail(1) for df in result.values() if df is not None and not df.empty]
            if tails:
                merged = pd.concat(tails, ignore_index=True)
                records = serialize_dataframe(merged)
            else:
                records = []
            self._fallback_cache = records
            self._fallback_time = time.time()
            logger.info("fallback query_snapshot 已缓存: %d 条", len(records))
        finally:
            self._fallback_lock.release()
        return self._filter_fallback(codes)

    def _filter_fallback(self, codes: list[str] | None) -> list[dict]:
        """在 fallback 缓存上按 codes 过滤，返回浅拷贝列表。缓存为空时返回 []。"""
        if not self._fallback_cache:
            return []
        if codes:
            wanted = set(codes)
            return [r for r in self._fallback_cache if r.get("code") in wanted]
        return list(self._fallback_cache)

    def _snapshot_to_dict(self, data) -> dict:
        """Snapshot 对象 → JSON 安全 dict（按类型缓存提取函数）。

        首次遇到某类型时确定提取策略（dataclass.asdict / vars / slots 遍历），
        缓存到 _extract_fns[type]，后续同类型直接调用，避免每帧 is_dataclass/hasattr 开销。
        提取后对每个值调 serialize_value 转换 datetime/NaN/numpy。
        """
        t = type(data)
        fn = self._extract_fns.get(t)
        if fn is None:
            fn = self._build_extract_fn(data)
            self._extract_fns[t] = fn
        raw = fn(data)
        return {k: serialize_value(v) for k, v in raw.items()}

    @staticmethod
    def _build_extract_fn(data):
        """根据 data 类型构建字段提取函数（首次调用，结果可缓存）。"""
        if dataclasses.is_dataclass(data):
            return dataclasses.asdict
        if hasattr(data, "__dict__"):
            return lambda d: dict(vars(d))
        slots = []
        for cls in type(data).__mro__:
            slots.extend(getattr(cls, "__slots__", []))
        if slots:
            return lambda d: {s: getattr(d, s) for s in slots if hasattr(d, s)}
        return lambda d: {}

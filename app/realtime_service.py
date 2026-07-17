"""RealtimeService：SDK SubscribeData 订阅回调 → 内存缓存 → GET /realtime 读缓存。

职责边界：
- on_snapshot 回调把 Snapshot 对象转为 JSON 安全 dict，按 code 覆盖写入 _cache
- snapshot 返回全市场快照列表（浅拷贝）
- 不做字段重命名/换算/衍生计算（与 /daily 哲学一致），由主项目 YAML field_map 处理

缓存语义：dict[code, dict] 覆盖写入，每个 code 只保留最新快照，无需淘汰策略。
"""

import dataclasses
import logging
import threading
import time

import pandas as pd

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

    def on_snapshot(self, data) -> None:
        """订阅回调：Snapshot → dict → 缓存覆盖。异常吞掉，不影响订阅线程。"""
        try:
            record = self._snapshot_to_dict(data)
            code = record.get("code") if record else None
            if code:
                with self._lock:
                    self._cache[code] = record
        except Exception as e:
            logger.warning("on_snapshot convert failed: %s: %s", type(e).__name__, e)

    def on_subscription_error(self, err=None) -> None:
        """订阅线程崩溃/异常退出回调：标记不活跃，/realtime 将返回 503。"""
        self._active = False
        logger.error("realtime subscription deactivated due to error: %s", err)

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
                logger.info("fallback no codes, returning cached: %d records", len(self._fallback_cache))
                return list(self._fallback_cache)
            if not self._gw.is_ready():
                raise GatewayNotReadyError("gateway not ready")
            logger.info("fallback no codes, cache empty, returning []")
            return []
        now = time.time()
        # 1. 缓存有效 → 直接返回过滤结果
        if self._fallback_cache is not None and now - self._fallback_time <= FALLBACK_TTL:
            logger.info("fallback cache hit (TTL valid): %d records", len(self._fallback_cache))
            return self._filter_fallback(codes)
        # 2. 缓存过期/空 → 非阻塞抢 singleflight 锁
        if not self._fallback_lock.acquire(blocking=False):
            # 已有查询在跑：返回旧缓存（哪怕过期）或空，不阻塞、不重复查询
            if self._fallback_cache:
                logger.info("fallback query in progress, returning stale cache: %d records",
                            len(self._fallback_cache))
                return self._filter_fallback(codes)
            logger.info("fallback query in progress, cache empty, returning []")
            return []
        try:
            # 双检：抢锁期间可能已被其他请求填充缓存
            now = time.time()
            if self._fallback_cache is not None and now - self._fallback_time <= FALLBACK_TTL:
                logger.info("fallback cache hit after lock: %d records", len(self._fallback_cache))
                return self._filter_fallback(codes)
            # codes 此处必非空（无 codes 已在方法开头短路返回），直接用作查询列表
            logger.info("fallback query_snapshot started: %d codes", len(codes))
            try:
                result = self._gw.query_snapshot(
                    codes,
                    begin_time=FALLBACK_BEGIN_TIME, end_time=FALLBACK_END_TIME,
                )
            except GatewayNotReadyError:
                raise
            except Exception as e:
                logger.warning("fallback query_snapshot failed: %s: %s", type(e).__name__, e)
                return self._filter_fallback(codes) if self._fallback_cache else []
            # 合并每只股票的最后一行（最新快照），一次 serialize_dataframe 序列化，
            # 避免几千只股票逐只调 serialize_dataframe 的开销。
            tails = [df.tail(1) for df in result.values() if df is not None and not df.empty]
            if tails:
                merged = pd.concat(tails, ignore_index=True)
                records = serialize_dataframe(merged)
            else:
                records = []
                logger.info("fallback query_snapshot returned empty result for %d codes", len(codes))
            self._fallback_cache = records
            self._fallback_time = time.time()
            logger.info("fallback query_snapshot: %d records cached", len(records))
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

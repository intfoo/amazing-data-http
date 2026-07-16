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

from app.gateway import GatewayNotReadyError
from app.serializer import serialize_dataframe, serialize_value

logger = logging.getLogger("amazingdata.realtime")

FALLBACK_TTL = 60  # 秒，fallback query_snapshot 缓存有效期（全市场查询较慢，避免每次请求都查）


class RealtimeService:
    def __init__(self, gateway):
        self._gw = gateway
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._active = False
        self._fallback_cache: list[dict] | None = None  # query_snapshot fallback 缓存
        self._fallback_time: float = 0
        # startup 注入的合并 code_list（股票+指数），供 fallback_snapshot 复用，避免每次重查 get_code_list
        self._combined_code_list: list[str] | None = None

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

        codes 为 None 时返回全市场快照；非空时只返回指定 code 的快照
        （从全市场缓存中过滤，不触发额外订阅）。
        对每个缓存 dict 做浅拷贝（dict(v)），避免外部序列化修改污染缓存。
        """
        with self._lock:
            if codes is None:
                return [dict(v) for v in self._cache.values()]
            wanted = set(codes)
            return [dict(v) for code, v in self._cache.items() if code in wanted]

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active

    def set_combined_code_list(self, codes: list[str]) -> None:
        """注入 startup 时获取的合并 code_list（股票+指数），供 fallback_snapshot 复用。

        避免每次 TTL 过期后重查 get_realtime_code_list（全市场查询 10~20s）。
        未调用此方法时 fallback_snapshot 会实时调 get_realtime_code_list（向后兼容）。
        """
        self._combined_code_list = codes

    def fallback_snapshot(self, codes: list[str] | None = None) -> list[dict]:
        """订阅缓存为空时的 fallback：用 query_snapshot 查当日历史快照。

        取每只股票的最后一行（最新/收盘快照）序列化返回。
        结果带 FALLBACK_TTL 秒缓存，避免每次请求都查 SDK（全市场查询较慢）。
        codes 过滤在缓存结果上应用。查询失败返回空列表（不抛异常，让 /realtime 返回空）。
        """
        now = time.time()
        if self._fallback_cache is None or now - self._fallback_time > FALLBACK_TTL:
            try:
                if codes:
                    code_list = codes
                elif self._combined_code_list is not None:
                    code_list = self._combined_code_list
                else:
                    code_list = self._gw.get_realtime_code_list()
            except GatewayNotReadyError:
                raise  # SDK 未就绪，传播给路由转 503
            except Exception as e:
                logger.warning("fallback get_realtime_code_list failed: %s: %s", type(e).__name__, e)
                return []
            try:
                result = self._gw.query_snapshot(code_list)
            except GatewayNotReadyError:
                raise
            except Exception as e:
                logger.warning("fallback query_snapshot failed: %s: %s", type(e).__name__, e)
                return []
            records: list[dict] = []
            for code, df in result.items():
                if df is None or df.empty:
                    continue
                # 取最后一行（最新快照），用 serialize_dataframe 序列化
                records.extend(serialize_dataframe(df.tail(1)))
            self._fallback_cache = records
            self._fallback_time = now
            logger.info("fallback query_snapshot: %d records cached", len(records))
        # 在缓存上按 codes 过滤
        if codes:
            wanted = set(codes)
            return [r for r in self._fallback_cache if r.get("code") in wanted]
        return list(self._fallback_cache)

    @staticmethod
    def _snapshot_to_dict(data) -> dict:
        """Snapshot 对象 → JSON 安全 dict。

        SDK 回调传入 ad.constant.Snapshot 对象，字段访问方式未在 probe 中验证。
        采用三级降级策略提取字段：
        1. dataclasses.is_dataclass(data) → dataclasses.asdict(data)
        2. 有 __dict__ → vars(data)
        3. 遍历 type(data).__mro__ 收集所有 __slots__（含继承）

        提取后对每个值调用 serialize_value 转换：
        - datetime/Timestamp → isoformat 字符串
        - NaN/NaT → None
        - numpy 标量 → python 原生
        返回 {} 表示无法提取（调用方 on_snapshot 跳过 code 为空的记录）。
        """
        if dataclasses.is_dataclass(data):
            raw = dataclasses.asdict(data)
        elif hasattr(data, "__dict__"):
            raw = dict(vars(data))
        else:
            slots = []
            for cls in type(data).__mro__:
                slots.extend(getattr(cls, "__slots__", []))
            raw = {s: getattr(data, s) for s in slots if hasattr(data, s)} if slots else {}
        return {k: serialize_value(v) for k, v in raw.items()}

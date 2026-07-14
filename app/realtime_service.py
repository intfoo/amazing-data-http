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

from app.serializer import serialize_value

logger = logging.getLogger("amazingdata.realtime")


class RealtimeService:
    def __init__(self, gateway):
        self._gw = gateway
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._active = False

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

    def snapshot(self) -> list[dict]:
        """GET /realtime 读缓存，返回全市场快照列表。

        对每个缓存 dict 做浅拷贝（dict(v)），避免外部序列化修改污染缓存。
        """
        with self._lock:
            return [dict(v) for v in self._cache.values()]

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active

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

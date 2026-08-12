"""ResilienceMixin：SDK 调用超时隔离与锁竞争超时保护。"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from app.gateway.base import logger, GatewayQueryError, SDK_LOCK_TIMEOUT_SEC


class ResilienceMixin:
    """SDK 调用弹性保障：超时隔离 daemon 线程 + _lock 竞争超时 contextmanager。"""

    def _call_sdk_with_timeout(self, fn, timeout_sec: float, label: str):
        """在独立 daemon 线程执行 SDK 同步调用，超时抛 GatewayQueryError。

        超时处置（防幽灵并发）：SDK 是无限期阻塞的 C 层调用，超时后幽灵线程仍在
        native 层执行。此处置 _ready=False（后续查询立即 503，不与幽灵线程并发进
        SDK）并触发 _schedule_reconnect 退避重建会话；幽灵线程在旧会话对象上自然
        终结（与断线重连场景等价）。
        异常消息用中文"超过 Ns 无响应"，刻意避开 _CONNECTION_KEYWORDS（timeout 等），
        防止上层 _is_connection_error 误匹配后在 _ready=False 状态下重复 _do_login+重试。
        fn 抛出的异常原样上抛（不包装），保持上层连接错误/损坏检测语义。
        """
        holder: dict = {}

        def _run() -> None:
            try:
                holder["result"] = fn()
            except Exception as e:  # noqa: BLE001 - SDK 异常需原样传递
                holder["error"] = e

        t = threading.Thread(target=_run, daemon=True, name=f"sdk-{label}")
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            self._ready = False
            try:
                self._schedule_reconnect(f"sdk call timeout: {label}")
            except Exception:  # 重连调度失败不掩盖原始超时错误
                pass
            raise GatewayQueryError(
                f"{label} 超过 {timeout_sec}s 无响应（SDK 线程已隔离为 daemon，会话重建中）"
            )
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    @contextmanager
    def _sdk_lock(self, timeout_sec: float = SDK_LOCK_TIMEOUT_SEC):
        """self._lock 的超时版本：挂起的 SDK 调用不再让后续请求无限排队假死。"""
        acquired = self._lock.acquire(timeout=timeout_sec)
        if not acquired:
            raise GatewayQueryError(
                f"gateway lock 竞争超时（{timeout_sec}s），存在挂起的 SDK 调用"
            )
        try:
            yield
        finally:
            self._lock.release()

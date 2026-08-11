"""ResilienceMixin：SDK 调用超时隔离与锁竞争超时保护。"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from app.gateway.base import logger, GatewayQueryError, SDK_LOCK_TIMEOUT_SEC


class ResilienceMixin:
    """SDK 调用弹性保障：超时隔离 daemon 线程 + _lock 竞争超时 contextmanager。"""

    @staticmethod
    def _call_sdk_with_timeout(fn, timeout_sec: float, label: str):
        """在独立 daemon 线程执行 SDK 同步调用，超时抛 GatewayQueryError。

        SDK 是无限期阻塞的 C 层调用（无 timeout 参数），Python 无法真正中断线程，
        超时后 SDK 线程作为 daemon 隔离（不再持有 gateway._lock，不阻塞后续调用；
        极端情况下 SDK 内部可能仍有残留状态，由 _is_sdk_corruption 重建机制兜底）。
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
            raise GatewayQueryError(
                f"{label} timed out after {timeout_sec}s（SDK 线程已隔离为 daemon）"
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

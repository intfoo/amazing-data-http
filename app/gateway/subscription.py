"""SubscriptionMixin：level-1 快照订阅启动与停止。"""

from __future__ import annotations

import threading

from app.gateway.base import logger, GatewayNotReadyError, GatewayQueryError


class SubscriptionMixin:
    """快照订阅管理：独立 daemon 线程跑 SubscribeData.run() 及停止。"""

    def start_snapshot_subscription(
        self, code_list: list[str], on_data, on_error=None
    ) -> None:
        """启动 level-1 快照订阅。在独立 daemon 线程跑 SubscribeData.run()。

        code_list: 订阅的证券代码列表（全市场，启动时固定）。
        on_data: 快照回调，签名 on_data(snapshot_obj)，由调用方处理缓存写入。
        on_error: 订阅线程异常退出时的回调，签名 on_error(exc)，用于通知调用方降级。
        Period 用 from AmazingData.utils.constant import Period（与 query_kline 一致）。
        持 _sdk_lock 串行化（订阅 start/stop 与查询一样触碰 SDK 内部状态）；
        sub.run 回调线程不持此锁，无死锁路径。
        """
        if not self._ready or self._ad is None:
            raise GatewayNotReadyError("gateway not ready for subscription")
        with self._sdk_lock():
            try:
                from AmazingData.utils.constant import Period
                sdk_period_value = Period.snapshot.value
            except Exception as e:
                raise GatewayQueryError(f"snapshot period mapping failed: {e}") from e

            sub = self._ad.SubscribeData()

            @sub.register(code_list=code_list, period=sdk_period_value)
            def _on_snapshot(data, period):
                try:
                    on_data(data)
                except Exception as e:
                    logger.warning("快照回调异常: %s: %s", type(e).__name__, e)

            self._subscribe_data = sub

            def _run():
                try:
                    sub.run()
                    # sub.run() 是无限循环(time.sleep(10))，正常情况下永不返回。
                    # 如果返回了，说明 SDK 内部出了问题（会话被踢/内部错误等）。
                    logger.error("SubscribeData.run() 异常返回（会话可能被踢）")
                    if on_error:
                        try:
                            on_error(RuntimeError("SubscribeData.run() returned unexpectedly"))
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("订阅线程崩溃: %s: %s", type(e).__name__, e)
                    if on_error:
                        try:
                            on_error(e)
                        except Exception:
                            pass

            self._sub_thread = threading.Thread(target=_run, daemon=True, name="snapshot-sub")
            self._sub_thread.start()
            logger.info("快照订阅已启动: %d 只", len(code_list))

    def stop_subscription(self) -> None:
        """停止订阅。SDK 若有 stop() 则调用，随后 join 订阅线程防止残留帧触发回调。

        持 _sdk_lock 串行化（订阅 start/stop 与查询一样触碰 SDK 内部状态）；
        sub.run 回调线程不持此锁，无死锁路径。
        """
        with self._sdk_lock():
            if self._subscribe_data is not None:
                try:
                    stop = getattr(self._subscribe_data, "stop", None)
                    if stop:
                        stop()
                except Exception as e:
                    logger.warning("停止订阅异常（已忽略）: %s: %s", type(e).__name__, e)
            thread = self._sub_thread
            self._subscribe_data = None
            self._sub_thread = None
        # join 订阅线程（锁外）：sub.run() 是 daemon 无限循环，stop() 可能未真正退出线程。
        # 不 join 会导致退订后残留帧继续触发 on_snapshot（盘后误复活订阅）。
        # 回调线程不持 _sdk_lock，join 最多 5s 放锁外执行，不阻塞其他 SDK 查询。
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("订阅线程 5s 内未退出（SDK stop() 可能无效），依赖窗口检查兜底")

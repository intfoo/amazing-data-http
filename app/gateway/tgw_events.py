"""TgwEventMixin：tgw 原生事件日志劫持与断线主动重连。"""

from __future__ import annotations

import sys
import threading
import time

from app.gateway.base import (
    _DISCONNECT_DEDUP_SEC,
    _RECONNECT_COOLDOWN_SEC,
    _TGW_NOISE_DEDUP_SEC,
    _TGW_NOISE_PATTERNS,
    logger,
)


class TgwEventMixin:
    """tgw 事件捕获：monkey-patch OnLog/OnEvent/OnLogon + 断线主动重连。"""

    def _schedule_reconnect(self, reason: str) -> None:
        """tgw 断线回调触发主动重连：后台线程执行 _do_login()，不阻塞 tgw 回调线程。

        指数退避：60→120→240→reconnect_max_interval_sec(默认300) 封顶，成功复位。
        故障期降低 ad.login 调用频率（每次失败 native 层疑似泄漏资源，OOM 防护）。
        """

        def _do() -> None:
            # 同步点：等待 _schedule_reconnect 退出 _reconnect_lock 后再执行，
            # 确保 _reconnect_in_progress=True 已对调用方可见。
            with self._reconnect_lock:
                pass
            # 让出 GIL 一拍，确保调用方线程（tgw 回调 / 测试）有机会读取
            # _reconnect_in_progress=True 后再继续重连逻辑。
            time.sleep(0)
            try:
                logger.info("tgw 断线触发主动重连: %s", reason)
                with self._sdk_lock():
                    self._do_login()
                with self._reconnect_lock:
                    self._reconnect_failures = 0
                logger.info("tgw 主动重连成功")
            except Exception as e:
                with self._reconnect_lock:
                    self._reconnect_failures += 1
                err = self._last_login_error or {}
                logger.error(
                    "tgw 主动重连失败: %s: %s (category=%s)",
                    type(e).__name__, e, err.get("category", "unknown"),
                )
            finally:
                with self._reconnect_lock:
                    self._reconnect_in_progress = False

        with self._reconnect_lock:
            if self._reconnect_in_progress:
                return
            now = time.time()
            interval = min(
                _RECONNECT_COOLDOWN_SEC * (2 ** self._reconnect_failures),
                self._config.reconnect_max_interval_sec,
            )
            if now - self._last_reconnect_attempt < interval:
                return
            self._reconnect_in_progress = True
            self._last_reconnect_attempt = now
            self._reconnect_attempts += 1
            # 在持锁状态下启动线程：_do 会先阻塞在 _reconnect_lock 上，
            # 直到本 with 块退出后才开始执行，保证调用方看到 _reconnect_in_progress=True。
            threading.Thread(target=_do, daemon=True, name="tgw-reconnect").start()

    def _should_log_disconnect(self, msg: str) -> bool:
        """断线 WARNING 去重：相同消息 _DISCONNECT_DEDUP_SEC 内只打一次。
        tgw 回调可能多线程分发，read-modify-write 需锁保护。"""
        with self._reconnect_lock:
            now = time.time()
            last = self._last_disconnect_log
            if msg == last["msg"] and now - last["ts"] < _DISCONNECT_DEDUP_SEC:
                return False
            last["msg"] = msg
            last["ts"] = now
            return True

    def _should_log_noise(self, msg: str) -> bool:
        """噪音日志 dedup：独立槽位（_last_noise_log），不与断线 WARNING 互相压制。"""
        with self._reconnect_lock:
            now = time.time()
            last = self._last_noise_log
            if msg == last["msg"] and now - last["ts"] < _TGW_NOISE_DEDUP_SEC:
                return False
            last["msg"] = msg
            last["ts"] = now
            return True

    def _install_login_spi_probe(self) -> None:
        """wrap AmazingData.login.tgw_login.set_cfg，捕获 log_spi 供失败分类。

        set_cfg 是模块级函数，login() 内经 LOAD_GLOBAL 运行时解析 → patch 模块属性生效。
        任何失败仅 warning 降级，不影响登录主流程。
        """
        try:
            from AmazingData.login import tgw_login
        except ImportError:
            return
        if getattr(tgw_login, "_spi_probe_installed", False):
            return
        original_set_cfg = tgw_login.set_cfg
        gateway = self

        def probed_set_cfg(*args, **kwargs):
            result = original_set_cfg(*args, **kwargs)
            try:
                gateway._last_login_spi = result[2]  # (cfg, api_mode, log_spi)
            except Exception:
                pass
            return result

        tgw_login.set_cfg = probed_set_cfg
        tgw_login._spi_probe_installed = True
        logger.info("tgw login spi probe installed on set_cfg")

    @property
    def reconnect_attempts(self) -> int:
        """累计主动重连次数（/health 诊断）。"""
        return self._reconnect_attempts

    def _install_tgw_event_logger(self) -> None:
        """Monkey-patch tgw.g_spi 的 OnLog/OnEvent/OnLogon 以捕获所有可能导致进程退出的事件。

        tgw 原生层在收到 force-logout 等致命事件时先调 OnLog，然后 native 线程直接调
        ExitProcess() 杀进程。Python 的 atexit/faulthandler/signal 都无法拦截 ExitProcess，
        但回调在 ExitProcess 之前被调用，可以在此打日志。

        已确认的退出路径：OnLog("RspForceLogout | release and exit now!!")
        其他可能的退出路径：OnEvent(kChannelTCPSessionClosed/kChannelTCPHeartbeatTimeout)
        OnLogon（登录状态变更，可能包含失败/被踢信息）
        """
        try:
            import tgw
        except ImportError:
            return

        spi = getattr(tgw, "g_spi", None)
        if spi is None:
            logger.warning("tgw.g_spi not found, cannot install event logger")
            return

        # 避免重复安装
        if getattr(spi, "_event_logger_installed", False):
            return

        original_on_log = spi.OnLog
        original_on_event = spi.OnEvent
        original_on_logon = spi.OnLogon

        # 预建 EventLevel / EventCode 反查表
        level_names = {}
        for attr in dir(tgw.EventLevel):
            if not attr.startswith("_"):
                level_names[getattr(tgw.EventLevel, attr)] = attr
        event_names = {}
        for attr in dir(tgw.EventCode):
            if not attr.startswith("_"):
                event_names[getattr(tgw.EventCode, attr)] = attr

        # 可能导致进程退出的关键词
        EXIT_KEYWORDS = ("ForceLogout", "force_logout", "release and exit",
                         "abort", "fatal", "FATAL")
        # 连接异常关键词（不一定导致退出，但值得关注）
        DISCONNECT_KEYWORDS = ("Disconnect", "disconnect", "SessionClosed",
                               "session_closed", "timeout", "Timeout",
                               "ConnectFailed", "connect_failed",
                               "LogonFailed", "logon_failed")

        def logged_on_log(level, log_msg=None, *args):
            """OnLog 回调：tgw 所有日志都走这里，包括 force-logout。

            sys.stderr.flush 仅在 FATAL/error 级别调用（确保 ExitProcess 前日志落盘），
            心跳/debug 级别不 flush，避免高频日志回调的系统调用开销。
            """
            msg_str = str(log_msg or "")
            level_name = level_names.get(level, str(level))

            # 登录窗口事件缓冲：login 失败时随 last_login_error 输出真实原因
            if self._login_in_progress:
                self._login_events.append(
                    {"kind": "log", "level": level_name, "msg": msg_str[:500]}
                )
                del self._login_events[:-20]

            if any(kw in msg_str for kw in EXIT_KEYWORDS):
                logger.error("tgw FATAL: [%s] %s (process may exit)", level_name, msg_str)
                sys.stderr.flush()
            elif any(kw in msg_str for kw in DISCONNECT_KEYWORDS):
                if self._should_log_disconnect(msg_str):
                    logger.warning("tgw disconnect: [%s] %s", level_name, msg_str)
                # 断线主动重连：原设计只有惰性重连（需新请求触发），
                # 非交易时段无请求时断线可挂 1 小时不自愈。
                self._schedule_reconnect(msg_str)
            elif level == 3:  # kError
                if any(p in msg_str for p in _TGW_NOISE_PATTERNS):
                    # tgw native 重连试 IP 等信息性消息被 SDK 错标 kError，降级 dedup
                    if self._should_log_noise(msg_str):
                        logger.info("tgw noise: [%s] %s", level_name, msg_str)
                elif "queue size" in msg_str or "in queue" in msg_str:
                    logger.debug("tgw push status: [%s] %s", level_name, msg_str)
                else:
                    logger.error("tgw error: [%s] %s", level_name, msg_str)
                    sys.stderr.flush()
            else:
                logger.debug("tgw [%s] %s", level_name, msg_str)

            try:
                original_on_log(level, log_msg, *args)
            except Exception:
                pass

        def logged_on_event(level, code, event_msg=None):
            """OnEvent 回调：连接状态变更事件。"""
            level_name = level_names.get(level, str(level))
            code_name = event_names.get(code, f"unknown({code})")
            logger.warning("tgw event: level=%s code=%s msg=%s",
                           level_name, code_name, event_msg or "")
            sys.stderr.flush()
            try:
                original_on_event(level, code, event_msg)
            except Exception:
                pass

        def logged_on_logon(data=None):
            """OnLogon 回调：登录状态变更（可能包含被踢/失败信息）。

            data 是 LogonResponse 对象（有 logon_json 属性），原始回调会调
            IGMDApi_FreeMemory(data) 释放它，所以必须在此之前提取信息。
            """
            info = ""
            if data is not None:
                try:
                    logon_json = getattr(data, "logon_json", None)
                    if logon_json:
                        # logon_json 可能很长，截取前 500 字符
                        info = f" logon_json={str(logon_json)[:500]}"
                except Exception:
                    info = " (failed to extract logon info)"
            if self._login_in_progress:
                self._login_events.append({"kind": "logon", "msg": info[:500]})
                del self._login_events[:-20]
            logger.warning("tgw logon event:%s", info or " (no details)")
            sys.stderr.flush()
            try:
                original_on_logon(data)
            except Exception:
                pass

        spi.OnLog = logged_on_log
        spi.OnEvent = logged_on_event
        spi.OnLogon = logged_on_logon
        spi._event_logger_installed = True
        logger.info("tgw event logger installed on OnLog + OnEvent + OnLogon")

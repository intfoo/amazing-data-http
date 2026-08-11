"""Gateway 层：SDK 调用边界封装。

HTTP 层只依赖 Gateway Protocol 接口，不感知 SDK 对象创建细节。
AmazingDataGateway 是真实实现，FakeGateway（tests/conftest.py）用于自动化测试。
所有 SDK 异常在此层转为 GatewayError 子类，上层只需 catch 统一基类。

线程安全：AmazingDataGateway 用 threading.Lock 串行化所有 SDK 调用，
因为 tgw 原生库的线程安全性未知，保守起见不支持并发查询。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

from app.config import Config

logger = logging.getLogger("amazingdata.gateway")


# 内部周期名 → SDK Period 枚举成员名的映射。
# HTTP 客户端不能直接传入任意整数，必须通过此白名单映射。
# 首期 /daily 固定使用 "day"，其余周期已建立映射但未对外暴露路由。
PERIOD_MAP: dict[str, str] = {
    "day": "day",
    "min1": "min1",
    "min3": "min3",
    "min5": "min5",
    "min10": "min10",
    "min15": "min15",
    "min30": "min30",
    "min60": "min60",
    "min120": "min120",
    "week": "week",
    "month": "month",
    "season": "season",
    "year": "year",
}


# 连接类错误关键词（小写匹配）。命中时触发惰性重连：持锁 relogin + 重试一次。
# 基于常见网络异常消息，保守匹配，误判也只是多一次 relogin 尝试。
_CONNECTION_KEYWORDS: tuple[str, ...] = (
    "connection", "timeout", "timed out", "disconnect", "disconnected",
    "broken pipe", "eof occurred", "reset", "unreachable", "refused", "closed",
)

_RECONNECT_COOLDOWN_SEC = 60    # 主动重连冷却（heartbeat 每 30s 报一次，避免频繁 relogin）
_DISCONNECT_DEDUP_SEC = 300     # 相同断线 WARNING 去重窗口

ADJ_FACTOR_TIMEOUT_SEC = 120  # get_adj_factor SDK 调用超时（正常本地 <1s / 远程 ~21s）


def _is_connection_error(exc: Exception) -> bool:
    """判断异常是否可能是网络/连接类错误（应触发重连）。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in _CONNECTION_KEYWORDS)


@runtime_checkable
class Gateway(Protocol):
    """Gateway 接口契约。HTTP 层只依赖此接口，测试用 FakeGateway 替换。"""

    def login(self) -> None: ...
    def logout(self) -> None: ...
    def is_ready(self) -> bool: ...
    def query_kline(
        self,
        codes: list[str],
        begin_date: int | None,
        end_date: int | None,
        period: str,
    ) -> dict[str, pd.DataFrame]: ...
    def refresh_calendar(self) -> list[int]: ...
    def get_code_list(self, security_type: str = "EXTRA_STOCK_A") -> list[str]: ...
    def get_realtime_code_list(self) -> list[str]: ...
    def query_snapshot(
        self, codes: list[str], trade_date: int | None = None,
        begin_time: int | None = None, end_time: int | None = None,
    ) -> dict[str, pd.DataFrame]: ...
    def start_snapshot_subscription(
        self, code_list: list[str], on_data, on_error=None
    ) -> None: ...
    def stop_subscription(self) -> None: ...
    def get_adj_factor(self, codes: list[str]) -> "pd.DataFrame": ...
    def get_code_info(self, security_type: str = "EXTRA_STOCK_A") -> "pd.DataFrame": ...
    def get_fund_share(
        self, codes: list[str],
        is_local: bool = False,
        begin_date: int | None = None, end_date: int | None = None,
    ) -> dict[str, "pd.DataFrame"]: ...
    def get_fund_nav(
        self, codes: list[str],
        is_local: bool = False,
        begin_date: int | None = None, end_date: int | None = None,
    ) -> dict[str, "pd.DataFrame"]: ...

    @property
    def calendar(self) -> list[int] | None: ...


class GatewayError(Exception):
    """Gateway 层所有异常的基类。"""
    pass


class GatewayNotReadyError(GatewayError):
    """SDK 未登录或未初始化，映射为 HTTP 503。"""
    pass


class GatewayQueryError(GatewayError):
    """SDK 查询失败或周期不支持，映射为 HTTP 502。"""
    pass


class AmazingDataGateway:
    """AmazingData SDK 的真实封装实现。

    管理进程级 SDK 会话：启动时 login + 创建 MarketData，
    后续请求复用同一会话，避免重复登录。SDK 对象非线程安全，
    所有操作通过 _lock 串行化。
    """

    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._ad = None           # AmazingData 模块引用
        self._market_data = None  # ad.MarketData 实例（含交易日历）
        self._ready = False       # 是否已登录且 MarketData 就绪
        self._base_data = None      # ad.BaseData 实例（供 get_code_list）
        self._calendar = None       # 交易日历 list[int]（供 query_snapshot 默认日期）
        self._subscribe_data = None  # ad.SubscribeData 实例
        self._sub_thread = None      # 订阅 daemon 线程
        self._adj_factor_local_path = self._resolve_adj_factor_local_path()
        self._info_data = None  # ad.InfoData 实例（供 get_fund_share/get_fund_nav）
        self._fund_local_path = self._resolve_fund_local_path()
        # 主动重连状态（tgw 断线回调触发，后台线程执行）
        self._reconnect_lock = threading.Lock()
        self._reconnect_in_progress = False
        self._last_reconnect_attempt = 0.0
        self._last_disconnect_log: dict = {"msg": None, "ts": 0.0}

    def _resolve_adj_factor_local_path(self) -> str:
        """解析 adj_factor 本地缓存基目录：配置非空用配置，否则用项目根 data 兜底 + 警告。

        启动时（实例化）调用，目录不存在则自动创建。SDK 要求绝对路径，且**末尾必须带分隔符**。

        SDK 字符串拼接坑（详见 base_data.pyc get_adj_factor 反汇编 + LocalDataFolder enum）：
        SDK 内部构造缓存路径用 `local_path + 'basedata/adj_factor/'`（字符串 +，非 os.path.join）：
            folder_name = LocalDataFolder.BASEDATA.value + '/' + LocalDataFolder.ADJ_FACTOR.value
            path = local_path + folder_name + '/'
        - local_path 末尾无分隔符（如 'D:/.../data/adj_factor'）→ 拼成 'D:/.../data/adj_factorbasedata/adj_factor/'
          （'adj_factor' 与 'basedata' 粘在一起，目录名错乱但能工作）
        - local_path 末尾带分隔符（如 'D:/.../data/'）→ 拼成 'D:/.../data/basedata/adj_factor/'（正确）
        手册 3.5.2.6 注(1) 示例 'D://AmazingData_local_data//' 末尾双斜杠正是为此设计。
        本方法强制末尾带分隔符，避免依赖调用方记忆此坑。

        兜底路径用 `项目根/data`（不带 `adj_factor` 后缀）：SDK 会自动在下面建
        `basedata/adj_factor/` 子目录，不需要在 local_path 里预设。若预设 `adj_factor` 后缀
        且末尾带分隔符，SDK 会拼出 `data/adj_factor/basedata/adj_factor/`（多一层 adj_factor）。
        """
        from pathlib import Path
        configured = self._config.adj_factor_local_path
        if configured:
            local_path = str(Path(configured).resolve())
        else:
            # 默认项目根/data（gateway.py 在 app/，parent.parent = 项目根）
            local_path = str(Path(__file__).resolve().parent.parent / "data")
            logger.warning(
                "ADJ_FACTOR_LOCAL_PATH 未配置，使用默认路径: %s"
                "（建议设为持久化绝对路径以供 SDK 缓存）",
                local_path,
            )
        # SDK 字符串拼接要求末尾带分隔符，否则 'data/adj_factor' + 'basedata' → 'adj_factorbasedata'
        if not local_path.endswith(('/', '\\')):
            local_path = local_path + '/'
        Path(local_path).mkdir(parents=True, exist_ok=True)
        return local_path

    def _resolve_fund_local_path(self) -> str:
        """解析 fund 本地缓存基目录：配置非空用配置，否则用项目根 data 兜底 + 警告。

        逻辑完全对标 _resolve_adj_factor_local_path()，读 self._config.fund_local_path。
        SDK get_fund_share/get_fund_nav 的 local_path 同样要求绝对路径且末尾带分隔符。
        """
        from pathlib import Path
        configured = self._config.fund_local_path
        if configured:
            local_path = str(Path(configured).resolve())
        else:
            # 默认项目根/data（gateway.py 在 app/，parent.parent = 项目根）
            local_path = str(Path(__file__).resolve().parent.parent / "data")
            logger.warning(
                "FUND_LOCAL_PATH 未配置，使用默认路径: %s"
                "（建议设为持久化绝对路径以供 SDK 缓存）",
                local_path,
            )
        # SDK 字符串拼接要求末尾带分隔符
        if not local_path.endswith(('/', '\\')):
            local_path = local_path + '/'
        Path(local_path).mkdir(parents=True, exist_ok=True)
        return local_path

    def login(self) -> None:
        """线程安全的登录入口。"""
        with self._lock:
            self._do_login()

    def _do_login(self) -> None:
        """实际登录流程：import SDK → login → BaseData → get_calendar → MarketData。

        SDK import 延迟到此处（而非模块顶部），使服务在无 SDK 环境下也能启动，
        /health 能正常返回 503 诊断信息。

        连接泄漏防护：ad.login() 成功后若后续步骤（BaseData/MarketData）失败，
        必须调 _safe_logout 释放已建立的 SDK 连接，否则 _ready 仍为 False，
        下次重 login 时不会先 logout（因 if self._ready 为 False），旧连接泄漏。
        """
        try:
            import AmazingData as ad
        except ImportError as e:
            logger.error("AmazingData SDK 导入失败: %s", e)
            self._ready = False
            raise GatewayNotReadyError(f"SDK import failed: {e}") from e

        self._ad = ad
        sdk_logged_in = False  # 标记 ad.login 是否已成功，用于失败时回滚
        try:
            if self._ready:
                self._safe_logout()
            ad.login(
                username=self._config.username,
                password=self._config.password,
                host=self._config.ip,
                port=self._config.port,
            )
            sdk_logged_in = True
            base = ad.BaseData()
            self._base_data = base
            calendar = base.get_calendar()
            self._calendar = calendar
            self._market_data = ad.MarketData(calendar)
            self._info_data = ad.InfoData()
            self._ready = True
            logger.info("SDK 登录成功")
            self._install_tgw_event_logger()
        except Exception as e:
            # ad.login 已成功但后续步骤失败：必须 logout 释放连接，否则连接泄漏
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("SDK 登录失败: %s: %s", type(e).__name__, e)
            raise GatewayNotReadyError(f"login failed: {e}") from e

    def _schedule_reconnect(self, reason: str) -> None:
        """tgw 断线回调触发主动重连：后台线程执行 _do_login()，不阻塞 tgw 回调线程。

        防重入（同时只有一个重连线程）+ 冷却（60s 内不重复尝试）。
        重连成功会顺带刷新交易日历（_do_login 重新 get_calendar）。
        """
        with self._reconnect_lock:
            if self._reconnect_in_progress:
                return
            now = time.time()
            if now - self._last_reconnect_attempt < _RECONNECT_COOLDOWN_SEC:
                return
            self._reconnect_in_progress = True
            self._last_reconnect_attempt = now

        def _do() -> None:
            try:
                logger.info("tgw 断线触发主动重连: %s", reason)
                with self._lock:
                    self._do_login()
                logger.info("tgw 主动重连成功")
            except Exception as e:
                logger.error("tgw 主动重连失败: %s: %s", type(e).__name__, e, exc_info=True)
            finally:
                with self._reconnect_lock:
                    self._reconnect_in_progress = False

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
                if "queue size" in msg_str or "in queue" in msg_str:
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

    def logout(self) -> None:
        """线程安全的登出入口。"""
        with self._lock:
            self._safe_logout()

    def _safe_logout(self) -> None:
        """登出并清理状态。登出异常被忽略（不影响后续重登录）。"""
        if self._ad is None:
            return
        try:
            self._ad.logout(username=self._config.username)
        except Exception as e:
            logger.warning("登出异常（已忽略）: %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None
        self._base_data = None
        self._info_data = None
        self._calendar = None

    def is_ready(self) -> bool:
        """SDK 是否已登录且 MarketData 已初始化。"""
        return self._ready

    @property
    def calendar(self) -> list[int] | None:
        """交易日历 list[int]（login 后可用，logout 后为 None）。"""
        return self._calendar

    def refresh_calendar(self) -> list[int]:
        """重新拉取交易日历并热更新到 MarketData.calendar 属性。

        根因背景：SDK query_kline 用 login 时快照的 calendar 本地过滤 date_list
        （market_data.pyc 字节码证实），日历不含查询日时 date_list 为空 →
        零网络请求 0.000s 静默返回 {}。长运行服务跨天后必须刷新日历。
        MarketData.calendar 是普通属性，直接赋值即热生效。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        with self._lock:
            calendar = self._base_data.get_calendar()
            self._calendar = calendar
            if self._market_data is not None:
                self._market_data.calendar = calendar
            logger.info(
                "交易日历已刷新: %d 天, 最新=%s",
                len(calendar), calendar[-1] if calendar else None,
            )
            return calendar

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

    def get_code_list(self, security_type: str = "EXTRA_STOCK_A") -> list[str]:
        """获取证券代码列表，委托 BaseData.get_code_list。未就绪抛 GatewayNotReadyError。

        加 _lock 串行化：tgw SDK 非线程安全，startup 订阅线程与 /realtime fallback
        线程并发调 get_code_list 会导致 'NoneType' object is not subscriptable
        （SDK 内部状态错乱）。串行化后并发调用排队，牺牲少量并发换取正确性。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        t_enter = time.perf_counter()
        with self._lock:
            t_lock = time.perf_counter()
            logger.debug(
                "get_code_list(security_type=%s) 调用 SDK (lock_wait=%.3fs)",
                security_type, t_lock - t_enter,
            )
            t0 = time.perf_counter()
            try:
                result = self._base_data.get_code_list(security_type=security_type)
            except Exception as e:
                logger.error("get_code_list 失败: %s: %s", type(e).__name__, e)
                raise GatewayQueryError(f"get_code_list failed: {e}") from e
            sdk_elapsed = time.perf_counter() - t0
            total_elapsed = time.perf_counter() - t_lock
            logger.info(
                "get_code_list(security_type=%s) 返回 %d 个代码 "
                "(sdk=%.3fs total=%.3fs)",
                security_type, len(result), sdk_elapsed, total_elapsed,
            )
            return result

    def get_code_info(self, security_type: str = "EXTRA_STOCK_A") -> "pd.DataFrame":
        """获取证券代码信息，委托 BaseData.get_code_info。未就绪抛 GatewayNotReadyError。

        模式同 get_code_list：检查 _ready/_base_data → 加 _lock 串行化 → 委托 SDK → 计时日志。
        返回 SDK 原始 DataFrame（index=证券代码, columns 含 symbol 等）。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        t_enter = time.perf_counter()
        with self._lock:
            t_lock = time.perf_counter()
            logger.debug(
                "get_code_info(security_type=%s) 调用 SDK (lock_wait=%.3fs)",
                security_type, t_lock - t_enter,
            )
            t0 = time.perf_counter()
            try:
                result = self._base_data.get_code_info(security_type=security_type)
            except Exception as e:
                logger.error("get_code_info 失败: %s: %s", type(e).__name__, e)
                raise GatewayQueryError(f"get_code_info failed: {e}") from e
            sdk_elapsed = time.perf_counter() - t0
            total_elapsed = time.perf_counter() - t_lock
            row_count = len(result) if result is not None else 0
            logger.info(
                "get_code_info(security_type=%s) 返回 %d 行 "
                "(sdk=%.3fs total=%.3fs)",
                security_type, row_count, sdk_elapsed, total_elapsed,
            )
            return result

    def get_realtime_code_list(self) -> list[str]:
        """获取实时订阅用的合并代码列表（股票 + 指数）。

        先取股票列表（EXTRA_STOCK_A），再取指数列表（EXTRA_INDEX_A）。
        股票列表获取失败时异常正常传播（GatewayNotReadyError / GatewayQueryError）。
        指数列表获取失败时降级：记录 warning，只返回股票列表，不抛异常。
        """
        t_total = time.perf_counter()
        stock_codes = self.get_code_list(security_type="EXTRA_STOCK_A")
        t_stock = time.perf_counter() - t_total
        try:
            t0 = time.perf_counter()
            index_codes = self.get_code_list(security_type="EXTRA_INDEX_A")
            t_index = time.perf_counter() - t0
        except Exception as e:
            logger.warning(
                "get_code_list(EXTRA_INDEX_A) 失败，降级为仅股票: %s: %s",
                type(e).__name__, e,
            )
            logger.info(
                "实时代码列表就绪: %d 只股票 (%.3fs，指数失败，总计 %.3fs)",
                len(stock_codes), t_stock, time.perf_counter() - t_total,
            )
            return stock_codes
        total = time.perf_counter() - t_total
        logger.info(
            "实时代码列表: %d 股票 + %d 指数 = %d "
            "(stock=%.3fs index=%.3fs total=%.3fs)",
            len(stock_codes), len(index_codes), len(stock_codes) + len(index_codes),
            t_stock, t_index, total,
        )
        return stock_codes + index_codes

    def query_snapshot(
        self,
        codes: list[str],
        trade_date: int | None = None,
        begin_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, "pd.DataFrame"]:
        """查询历史快照。返回 {code: DataFrame}（每只股票当日全部快照行，按时间排列）。

        trade_date 为 None 时用交易日历最后一天（最新交易日）。
        begin_time / end_time 为可选时分秒毫秒时间戳（如 9点整=90000000，15点=150000000），
        传入时只返回该时间区间内的快照行，避免拉全量逐笔（默认返回当日全部，每只5000+行）。
        SDK 返回嵌套 dict {date: {code: DataFrame}}，此处展平取内层 {code: DataFrame}。
        用于 /realtime 订阅缓存为空（非交易时段）时的 fallback。
        """
        if not self._ready or self._market_data is None:
            raise GatewayNotReadyError("gateway not ready")
        if trade_date is None:
            if not self._calendar:
                raise GatewayNotReadyError("calendar not available")
            trade_date = self._calendar[-1]
        # 仅传非 None 的时间参数，None 时让 SDK 返回当日全部快照
        kwargs: dict[str, Any] = {"begin_date": trade_date, "end_date": trade_date}
        if begin_time is not None:
            kwargs["begin_time"] = begin_time
        if end_time is not None:
            kwargs["end_time"] = end_time
        with self._lock:
            try:
                result = self._market_data.query_snapshot(codes, **kwargs)
            except Exception as e:
                logger.error("query_snapshot 失败: %s: %s (codes=%d, date=%s)",
                             type(e).__name__, e, len(codes), trade_date)
                if _is_connection_error(e):
                    logger.warning("query_snapshot 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_snapshot(codes, **kwargs)
                        logger.info("query_snapshot 重连后成功")
                    except Exception as e2:
                        logger.error("query_snapshot 重连后仍失败: %s: %s", type(e2).__name__, e2)
                        raise GatewayQueryError(f"query_snapshot failed after reconnect: {e2}") from e2
                else:
                    raise GatewayQueryError(f"query_snapshot failed: {e}") from e
        # 展平嵌套 {date: {code: DataFrame}} → {code: DataFrame}
        flat: dict[str, pd.DataFrame] = {}
        if isinstance(result, dict):
            for _date, inner in result.items():
                if isinstance(inner, dict):
                    for code, df in inner.items():
                        if df is not None and not df.empty:
                            flat[code] = df
                elif inner is not None and hasattr(inner, "empty") and not inner.empty:
                    flat["_all"] = inner
        return flat

    def query_kline(
        self,
        codes: list[str],
        begin_date: int | None,
        end_date: int | None,
        period: str,
    ) -> dict[str, "pd.DataFrame"]:
        """查询 K 线数据。period 是内部字符串（如 "day"），通过 PERIOD_MAP 映射到 SDK 枚举。

        begin_date / end_date 为 None 时不传给 SDK，由 SDK 使用默认区间
        （begin_date 默认 20240101，end_date 默认 20991231）。
        返回 dict[code, DataFrame]。若 SDK 返回非 dict（如单个 DataFrame），
        用 {"_all": result} 包装以统一接口。
        """
        if not self._ready or self._market_data is None:
            raise GatewayNotReadyError("gateway not ready")
        # 日历过期防护：end_date 超出日历最后一天时先热刷新，否则 SDK 本地过滤后
        # date_list 为空，0.000s 静默返回空（无网络请求、无异常，极难排查）。
        if end_date is not None and self._calendar and end_date > self._calendar[-1]:
            logger.info(
                "query_kline end_date=%s 超出日历最后一天 %s，先刷新交易日历",
                end_date, self._calendar[-1],
            )
            self.refresh_calendar()
        sdk_period_name = PERIOD_MAP.get(period)
        if sdk_period_name is None:
            raise GatewayQueryError(f"unsupported period: {period}")
        try:
            from AmazingData.utils.constant import Period
            sdk_period_value = getattr(Period, sdk_period_name).value
        except Exception as e:
            raise GatewayQueryError(f"period mapping failed: {e}") from e

        # 仅传非 None 的日期参数，None 时让 SDK 用默认值
        kwargs: dict[str, Any] = {"period": sdk_period_value}
        if begin_date is not None:
            kwargs["begin_date"] = begin_date
        if end_date is not None:
            kwargs["end_date"] = end_date

        with self._lock:
            try:
                result = self._market_data.query_kline(codes, **kwargs)
                return result if isinstance(result, dict) else {"_all": result}
            except Exception as e:
                logger.error(
                    "query_kline 失败: %s: %s (codes=%d, begin=%s, end=%s, period=%s)",
                    type(e).__name__, e, len(codes),
                    begin_date if begin_date is not None else "default",
                    end_date if end_date is not None else "default",
                    period,
                )
                if _is_connection_error(e):
                    logger.warning("query_kline 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_kline(codes, **kwargs)
                        logger.info("query_kline 重连后成功")
                        return result if isinstance(result, dict) else {"_all": result}
                    except Exception as e2:
                        logger.error("query_kline 重连后仍失败: %s: %s", type(e2).__name__, e2)
                        raise GatewayQueryError(f"query failed after reconnect: {e2}") from e2
                raise GatewayQueryError(f"query failed: {e}") from e

    def start_snapshot_subscription(
        self, code_list: list[str], on_data, on_error=None
    ) -> None:
        """启动 level-1 快照订阅。在独立 daemon 线程跑 SubscribeData.run()。

        code_list: 订阅的证券代码列表（全市场，启动时固定）。
        on_data: 快照回调，签名 on_data(snapshot_obj)，由调用方处理缓存写入。
        on_error: 订阅线程异常退出时的回调，签名 on_error(exc)，用于通知调用方降级。
        Period 用 from AmazingData.utils.constant import Period（与 query_kline 一致）。
        """
        if not self._ready or self._ad is None:
            raise GatewayNotReadyError("gateway not ready for subscription")
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
        """停止订阅。SDK 若有 stop() 则调用，随后 join 订阅线程防止残留帧触发回调。"""
        if self._subscribe_data is not None:
            try:
                stop = getattr(self._subscribe_data, "stop", None)
                if stop:
                    stop()
            except Exception as e:
                logger.warning("停止订阅异常（已忽略）: %s: %s", type(e).__name__, e)
        # join 订阅线程：sub.run() 是 daemon 无限循环，stop() 可能未真正退出线程。
        # 不 join 会导致退订后残留帧继续触发 on_snapshot（盘后误复活订阅）。
        thread = self._sub_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("订阅线程 5s 内未退出（SDK stop() 可能无效），依赖窗口检查兜底")
        self._subscribe_data = None
        self._sub_thread = None

    def get_adj_factor(self, codes: list[str]) -> "pd.DataFrame":
        """获取单次复权因子（手册 3.5.2.6）。返回 SDK 原始 DataFrame（宽表：index=交易日期, columns=股票代码）。

        SDK 签名 get_adj_factor(code_list, local_path, is_local)，无日期参数。
        is_local 由 Config.adj_factor_is_local 控制（环境变量 ADJ_FACTOR_IS_LOCAL）：
        - False（默认）：每次从服务端取最新，仍会更新 local_path 缓存（手册注(2)）。每次 ~21s。
        - True：本地有缓存则读本地（<1s），本地无则远程取 + 写本地（首次 ~21s）。
          风险：本地缓存可能陈旧（adj_factor 除权事件一年几次，风险低但不为零）。
        local_path 在启动时由 _resolve_adj_factor_local_path 解析（配置优先，否则项目根 data 兜底，
        SDK 会自建 basedata/adj_factor/ 子目录）。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        is_local = self._config.adj_factor_is_local
        with self._lock:
            try:
                result = self._call_sdk_with_timeout(
                    lambda: self._base_data.get_adj_factor(
                        codes,
                        local_path=self._adj_factor_local_path,
                        is_local=is_local,
                    ),
                    ADJ_FACTOR_TIMEOUT_SEC,
                    "get_adj_factor",
                )
            except Exception as e:
                logger.error("get_adj_factor 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local, exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_adj_factor 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._call_sdk_with_timeout(
                            lambda: self._base_data.get_adj_factor(
                                codes,
                                local_path=self._adj_factor_local_path,
                                is_local=is_local,
                            ),
                            ADJ_FACTOR_TIMEOUT_SEC,
                            "get_adj_factor",
                        )
                        logger.info("get_adj_factor 重连后成功")
                    except Exception as e2:
                        logger.error("get_adj_factor 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_adj_factor failed after reconnect: {e2}") from e2
                else:
                    raise GatewayQueryError(f"get_adj_factor failed: {e}") from e
            # SDK 内部状态损坏时可能静默返回 None（如本地 HDF5 缓存损坏，
            # 下游 df[...] 直接 TypeError）。is_local=True 时回退远程重试一次。
            if result is None:
                if is_local:
                    logger.warning(
                        "get_adj_factor is_local=True 返回 None（本地缓存可能损坏），"
                        "回退 is_local=False 远程重试"
                    )
                    result = self._call_sdk_with_timeout(
                        lambda: self._base_data.get_adj_factor(
                            codes,
                            local_path=self._adj_factor_local_path,
                            is_local=False,
                        ),
                        ADJ_FACTOR_TIMEOUT_SEC,
                        "get_adj_factor",
                    )
                if result is None:
                    raise GatewayQueryError(
                        "get_adj_factor returned None（SDK 内部错误，"
                        "建议删除本地 adj_factor 缓存后重试）"
                    )
            return result

    def get_fund_share(
        self,
        codes: list[str],
        is_local: bool = False,
        begin_date: int | None = None,
        end_date: int | None = None,
    ) -> dict[str, "pd.DataFrame"]:
        """获取基金/ETF 份额历史时序，委托 InfoData.get_fund_share。

        模式对标 get_adj_factor：检查 _ready/_info_data → 加 _lock 串行化 → 委托 SDK → 连接错误重连。
        local_path 由 Gateway 内部从 Config.fund_local_path 读取（_resolve_fund_local_path 解析），
        is_local 由 Config.fund_is_local 控制（Protocol 签名保留 is_local 仅为接口契约明确性）。
        返回 dict[code, DataFrame]（DataFrame 含 FUND_SHARE, CHANGE_DATE 等列）。
        """
        if not self._ready or self._info_data is None:
            raise GatewayNotReadyError("gateway not ready")
        is_local = self._config.fund_is_local
        with self._lock:
            try:
                return self._info_data.get_fund_share(
                    codes,
                    local_path=self._fund_local_path,
                    is_local=is_local,
                    begin_date=begin_date,
                    end_date=end_date,
                )
            except Exception as e:
                logger.error("get_fund_share 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local)
                if _is_connection_error(e):
                    logger.warning("get_fund_share 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._info_data.get_fund_share(
                            codes,
                            local_path=self._fund_local_path,
                            is_local=is_local,
                            begin_date=begin_date,
                            end_date=end_date,
                        )
                        logger.info("get_fund_share 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_fund_share 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2)
                        raise GatewayQueryError(f"get_fund_share failed after reconnect: {e2}") from e2
                raise GatewayQueryError(f"get_fund_share failed: {e}") from e

    def get_fund_nav(
        self,
        codes: list[str],
        is_local: bool = False,
        begin_date: int | None = None,
        end_date: int | None = None,
    ) -> dict[str, "pd.DataFrame"]:
        """获取基金/ETF 净值历史时序，委托 InfoData.get_fund_nav。

        模式同 get_fund_share：检查 _ready/_info_data → 加 _lock 串行化 → 委托 SDK → 连接错误重连。
        local_path 由 Gateway 内部从 Config.fund_local_path 读取，is_local 由 Config.fund_is_local 控制。
        返回 dict[code, DataFrame]（DataFrame 含 UNIT_NAV, PRICE_DATE 等列）。
        """
        if not self._ready or self._info_data is None:
            raise GatewayNotReadyError("gateway not ready")
        is_local = self._config.fund_is_local
        with self._lock:
            try:
                return self._info_data.get_fund_nav(
                    codes,
                    local_path=self._fund_local_path,
                    is_local=is_local,
                    begin_date=begin_date,
                    end_date=end_date,
                )
            except Exception as e:
                logger.error("get_fund_nav 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local)
                if _is_connection_error(e):
                    logger.warning("get_fund_nav 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._info_data.get_fund_nav(
                            codes,
                            local_path=self._fund_local_path,
                            is_local=is_local,
                            begin_date=begin_date,
                            end_date=end_date,
                        )
                        logger.info("get_fund_nav 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_fund_nav 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2)
                        raise GatewayQueryError(f"get_fund_nav failed after reconnect: {e2}") from e2
                raise GatewayQueryError(f"get_fund_nav failed: {e}") from e

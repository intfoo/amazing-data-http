"""Gateway 层：SDK 调用边界封装。

HTTP 层只依赖 Gateway Protocol 接口，不感知 SDK 对象创建细节。
AmazingDataGateway 是真实实现，FakeGateway（tests/conftest.py）用于自动化测试。
所有 SDK 异常在此层转为 GatewayError 子类，上层只需 catch 统一基类。

线程安全：AmazingDataGateway 用 threading.Lock 串行化所有 SDK 调用，
因为 tgw 原生库的线程安全性未知，保守起见不支持并发查询。
"""

import logging
import threading
from typing import Any, Protocol, runtime_checkable

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
            logger.error("AmazingData import failed: %s", e)
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
            self._ready = True
            logger.info("AmazingData gateway login successful")
        except Exception as e:
            # ad.login 已成功但后续步骤失败：必须 logout 释放连接，否则连接泄漏
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("AmazingData login failed: %s: %s", type(e).__name__, e)
            raise GatewayNotReadyError(f"login failed: {e}") from e

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
            logger.warning("logout error (ignored): %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None
        self._base_data = None
        self._calendar = None

    def is_ready(self) -> bool:
        """SDK 是否已登录且 MarketData 已初始化。"""
        return self._ready

    def get_code_list(self, security_type: str = "EXTRA_STOCK_A") -> list[str]:
        """获取证券代码列表，委托 BaseData.get_code_list。未就绪抛 GatewayNotReadyError。

        加 _lock 串行化：tgw SDK 非线程安全，startup 订阅线程与 /realtime fallback
        线程并发调 get_code_list 会导致 'NoneType' object is not subscriptable
        （SDK 内部状态错乱）。串行化后并发调用排队，牺牲少量并发换取正确性。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        with self._lock:
            try:
                return self._base_data.get_code_list(security_type=security_type)
            except Exception as e:
                logger.error("get_code_list failed: %s: %s", type(e).__name__, e)
                raise GatewayQueryError(f"get_code_list failed: {e}") from e

    def get_realtime_code_list(self) -> list[str]:
        """获取实时订阅用的合并代码列表（股票 + 指数）。

        先取股票列表（EXTRA_STOCK_A），再取指数列表（EXTRA_INDEX_A）。
        股票列表获取失败时异常正常传播（GatewayNotReadyError / GatewayQueryError）。
        指数列表获取失败时降级：记录 warning，只返回股票列表，不抛异常。
        """
        stock_codes = self.get_code_list(security_type="EXTRA_STOCK_A")
        try:
            index_codes = self.get_code_list(security_type="EXTRA_INDEX_A")
        except Exception as e:
            logger.warning(
                "get_code_list(EXTRA_INDEX_A) failed, degrading to stock-only: %s: %s",
                type(e).__name__, e,
            )
            return stock_codes
        logger.info("get_realtime_code_list: %d stocks + %d indices = %d total",
                    len(stock_codes), len(index_codes), len(stock_codes) + len(index_codes))
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
                logger.error("query_snapshot failed: %s: %s (codes=%d, date=%s)",
                             type(e).__name__, e, len(codes), trade_date)
                if _is_connection_error(e):
                    logger.warning("query_snapshot connection error, attempting relogin: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_snapshot(codes, **kwargs)
                        logger.info("query_snapshot succeeded after relogin")
                    except Exception as e2:
                        logger.error("query_snapshot failed after reconnect: %s: %s", type(e2).__name__, e2)
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
                    "query_kline failed: %s: %s (codes=%d, begin=%s, end=%s, period=%s)",
                    type(e).__name__, e, len(codes),
                    begin_date if begin_date is not None else "default",
                    end_date if end_date is not None else "default",
                    period,
                )
                if _is_connection_error(e):
                    logger.warning("query_kline connection error, attempting relogin: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_kline(codes, **kwargs)
                        logger.info("query_kline succeeded after relogin")
                        return result if isinstance(result, dict) else {"_all": result}
                    except Exception as e2:
                        logger.error("query_kline failed after reconnect: %s: %s", type(e2).__name__, e2)
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
                logger.warning("snapshot callback error: %s: %s", type(e).__name__, e)

        self._subscribe_data = sub

        def _run():
            try:
                sub.run()
            except Exception as e:
                logger.error("subscription thread crashed: %s: %s", type(e).__name__, e)
                if on_error:
                    try:
                        on_error(e)
                    except Exception:
                        pass

        self._sub_thread = threading.Thread(target=_run, daemon=True, name="snapshot-sub")
        self._sub_thread.start()
        logger.info("snapshot subscription started: %d symbols", len(code_list))

    def stop_subscription(self) -> None:
        """停止订阅。SDK 若有 stop() 则调用，daemon 线程随进程退出。清理引用。"""
        if self._subscribe_data is not None:
            try:
                stop = getattr(self._subscribe_data, "stop", None)
                if stop:
                    stop()
            except Exception as e:
                logger.warning("stop subscription error (ignored): %s: %s", type(e).__name__, e)
        self._subscribe_data = None
        self._sub_thread = None

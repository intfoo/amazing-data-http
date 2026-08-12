"""行情查询 Mixin：代码列表、快照、K 线等市场数据查询。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

from app.gateway.base import (
    PERIOD_MAP,
    GatewayNotReadyError,
    GatewayQueryError,
    _is_connection_error,
    _is_sdk_corruption,
    logger,
)


class QueryMarketMixin:
    """行情数据查询职责 Mixin：代码列表、实时代码、快照、K 线。"""

    def get_code_list(self, security_type: str = "EXTRA_STOCK_A") -> list[str]:
        """获取证券代码列表，委托 BaseData.get_code_list。未就绪抛 GatewayNotReadyError。

        加 _lock 串行化：tgw SDK 非线程安全，startup 订阅线程与 /realtime fallback
        线程并发调 get_code_list 会导致 'NoneType' object is not subscriptable
        （SDK 内部状态错乱）。串行化后并发调用排队，牺牲少量并发换取正确性。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        t_enter = time.perf_counter()
        with self._sdk_lock():
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
        with self._sdk_lock():
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

    def get_realtime_universe(self) -> dict[str, str]:
        """获取实时订阅用的全市场代码表（股票 + 指数 + ETF），返回 {code: type}。

        依次取 EXTRA_STOCK_A → EXTRA_INDEX_A → EXTRA_ETF，合并为 dict[code, "stock"|"index"|"etf"]。
        股票列表获取失败时异常正常传播（GatewayNotReadyError / GatewayQueryError）。
        指数 / ETF 列表获取失败时各自独立降级：记录 warning，跳过该类，不影响其余类别。
        """
        t_total = time.perf_counter()
        universe: dict[str, str] = {}

        # 股票（EXTRA_STOCK_A）：失败异常传播
        stock_codes = self.get_code_list(security_type="EXTRA_STOCK_A")
        t_stock = time.perf_counter() - t_total
        universe.update({c: "stock" for c in stock_codes})

        # 指数（EXTRA_INDEX_A）：失败 warn 降级
        try:
            t0 = time.perf_counter()
            index_codes = self.get_code_list(security_type="EXTRA_INDEX_A")
            t_index = time.perf_counter() - t0
        except Exception as e:
            logger.warning(
                "get_code_list(EXTRA_INDEX_A) 失败，降级为仅股票+ETF: %s: %s",
                type(e).__name__, e,
            )
            logger.info(
                "实时代码列表就绪: %d 股票 + 0 指数 + 0 ETF = %d "
                "(stock=%.3fs，指数失败，总计 %.3fs)",
                len(stock_codes), len(universe),
                t_stock, time.perf_counter() - t_total,
            )
            return universe
        universe.update({c: "index" for c in index_codes})

        # ETF（EXTRA_ETF）：失败 warn 降级
        try:
            t0 = time.perf_counter()
            etf_codes = self.get_code_list(security_type="EXTRA_ETF")
            t_etf = time.perf_counter() - t0
        except Exception as e:
            logger.warning(
                "get_code_list(EXTRA_ETF) 失败，降级为仅股票+指数: %s: %s",
                type(e).__name__, e,
            )
            logger.info(
                "实时代码列表就绪: %d 股票 + %d 指数 + 0 ETF = %d "
                "(stock=%.3fs index=%.3fs，ETF失败，总计 %.3fs)",
                len(stock_codes), len(index_codes), len(universe),
                t_stock, t_index, time.perf_counter() - t_total,
            )
            return universe
        universe.update({c: "etf" for c in etf_codes})

        total = time.perf_counter() - t_total
        logger.info(
            "实时代码列表: %d 股票 + %d 指数 + %d ETF = %d "
            "(stock=%.3fs index=%.3fs etf=%.3fs total=%.3fs)",
            len(stock_codes), len(index_codes), len(etf_codes), len(universe),
            t_stock, t_index, t_etf, total,
        )
        return universe

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
        with self._sdk_lock():
            try:
                result = self._market_data.query_snapshot(codes, **kwargs)
            except Exception as e:
                logger.error("query_snapshot 失败: %s: %s (codes=%d, date=%s)",
                             type(e).__name__, e, len(codes), trade_date,
                             exc_info=True)
                if _is_connection_error(e):
                    logger.warning("query_snapshot 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_snapshot(codes, **kwargs)
                        logger.info("query_snapshot 重连后成功")
                    except Exception as e2:
                        logger.error("query_snapshot 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"query_snapshot failed after reconnect: {e2}") from e2
                else:
                    if _is_sdk_corruption(e):
                        logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                        try:
                            self._do_login()
                        except Exception as e3:
                            logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
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

        with self._sdk_lock():
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
                    exc_info=True,
                )
                if _is_connection_error(e):
                    logger.warning("query_kline 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._market_data.query_kline(codes, **kwargs)
                        logger.info("query_kline 重连后成功")
                        return result if isinstance(result, dict) else {"_all": result}
                    except Exception as e2:
                        logger.error("query_kline 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"query failed after reconnect: {e2}") from e2
                if _is_sdk_corruption(e):
                    # SDK 异常路径不释放内部 lock（market_data.pyc 字节码证实），
                    # 不重建会让后续所有查询在 SDK lock.acquire() 上永久挂起。
                    logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                raise GatewayQueryError(f"query failed: {e}") from e

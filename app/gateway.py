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


@runtime_checkable
class Gateway(Protocol):
    """Gateway 接口契约。HTTP 层只依赖此接口，测试用 FakeGateway 替换。"""

    def login(self) -> None: ...
    def logout(self) -> None: ...
    def is_ready(self) -> bool: ...
    def query_kline(
        self,
        symbols: list[str],
        begin_date: int,
        end_date: int,
        period: str,
    ) -> dict[str, pd.DataFrame]: ...


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
    """AmazingData SDK 1.1.7 的真实封装实现。

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

    def login(self) -> None:
        """线程安全的登录入口。"""
        with self._lock:
            self._do_login()

    def _do_login(self) -> None:
        """实际登录流程：import SDK → login → BaseData → get_calendar → MarketData。

        SDK import 延迟到此处（而非模块顶部），使服务在无 SDK 环境下也能启动，
        /health 能正常返回 503 诊断信息。
        """
        try:
            import AmazingData as ad
        except ImportError as e:
            logger.error("AmazingData import failed: %s", e)
            self._ready = False
            raise GatewayNotReadyError(f"SDK import failed: {e}") from e

        self._ad = ad
        try:
            if self._ready:
                self._safe_logout()
            ad.login(
                username=self._config.username,
                password=self._config.password,
                ip=self._config.ip,
                port=self._config.port,
            )
            base = ad.BaseData()
            calendar = base.get_calendar()
            self._market_data = ad.MarketData(calendar)
            self._ready = True
            logger.info("AmazingData gateway login successful")
        except Exception as e:
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
            self._ad.logout()
        except Exception as e:
            logger.warning("logout error (ignored): %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None

    def is_ready(self) -> bool:
        """SDK 是否已登录且 MarketData 已初始化。"""
        return self._ready

    def query_kline(
        self,
        symbols: list[str],
        begin_date: int,
        end_date: int,
        period: str,
    ) -> dict[str, "pd.DataFrame"]:
        """查询 K 线数据。period 是内部字符串（如 "day"），通过 PERIOD_MAP 映射到 SDK 枚举。

        返回 dict[code, DataFrame]。若 SDK 返回非 dict（如单个 DataFrame），
        用 {"_all": result} 包装以统一接口。
        """
        if not self._ready or self._market_data is None:
            raise GatewayNotReadyError("gateway not ready")
        sdk_period_name = PERIOD_MAP.get(period)
        if sdk_period_name is None:
            raise GatewayQueryError(f"unsupported period: {period}")
        try:
            from AmazingData.constant import Period
            sdk_period_value = getattr(Period, sdk_period_name).value
        except Exception as e:
            raise GatewayQueryError(f"period mapping failed: {e}") from e

        with self._lock:
            try:
                result = self._market_data.query_kline(
                    symbols,
                    begin_date=begin_date,
                    end_date=end_date,
                    period=sdk_period_value,
                )
                return result if isinstance(result, dict) else {"_all": result}
            except Exception as e:
                # 日志记录上下文（代码数量、日期区间），不记录完整代码列表和密码
                logger.error(
                    "query_kline failed: %s: %s (symbols=%d, begin=%d, end=%d, period=%s)",
                    type(e).__name__, e, len(symbols), begin_date, end_date, period,
                )
                raise GatewayQueryError(f"query failed: {e}") from e

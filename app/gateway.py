import logging
import threading
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from app.config import Config

logger = logging.getLogger("amazingdata.gateway")


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
    pass


class GatewayNotReadyError(GatewayError):
    pass


class GatewayQueryError(GatewayError):
    pass


class AmazingDataGateway:
    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._ad = None
        self._market_data = None
        self._ready = False

    def login(self) -> None:
        with self._lock:
            self._do_login()

    def _do_login(self) -> None:
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
        with self._lock:
            self._safe_logout()

    def _safe_logout(self) -> None:
        if self._ad is None:
            return
        try:
            self._ad.logout()
        except Exception as e:
            logger.warning("logout error (ignored): %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None

    def is_ready(self) -> bool:
        return self._ready

    def query_kline(
        self,
        symbols: list[str],
        begin_date: int,
        end_date: int,
        period: str,
    ) -> dict[str, "pd.DataFrame"]:
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
                logger.error(
                    "query_kline failed: %s: %s (symbols=%d, begin=%d, end=%d, period=%s)",
                    type(e).__name__, e, len(symbols), begin_date, end_date, period,
                )
                raise GatewayQueryError(f"query failed: {e}") from e

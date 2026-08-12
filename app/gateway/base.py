"""Gateway 基础定义：logger、常量、异常、Protocol。拆分自原 app/gateway.py，行为零变化。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

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

_RECONNECT_MAX_INTERVAL_SEC = 300   # 文档性默认值标注，实际读取 Config.reconnect_max_interval_sec
_TGW_NOISE_PATTERNS: tuple[str, ...] = ("HandleFile", "Now use ip", "mdga.json")
_TGW_NOISE_DEDUP_SEC = 60           # tgw 噪音日志 dedup 窗口

ADJ_FACTOR_TIMEOUT_SEC = 120  # get_adj_factor SDK 调用超时（正常本地 <1s / 远程 ~21s）

SDK_LOCK_TIMEOUT_SEC = 30  # gateway._lock 竞争超时；超时说明有 SDK 调用挂起未释放


def _is_connection_error(exc: Exception) -> bool:
    """判断异常是否可能是网络/连接类错误（应触发重连）。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in _CONNECTION_KEYWORDS)


# SDK 内部状态损坏关键词（'查询失败' 是 SDK pyc 内硬编码的中文异常消息，
# 见 market_data.pyc 反汇编；'NoneType' 见于 get_code_list/get_adj_factor 内部状态错乱）。
# 命中说明 SDK 内部状态机/锁已损坏（SDK 异常路径不释放内部 lock），
# 必须 _do_login() 重建会话（新实例=新锁），否则后续所有查询永久挂起。
_SDK_CORRUPTION_KEYWORDS: tuple[str, ...] = ("查询失败", "NoneType")


def _is_sdk_corruption(exc: Exception) -> bool:
    """判断异常是否是 SDK 内部状态损坏（非网络问题，重试无意义，需重建会话）。"""
    msg = str(exc)
    return any(kw in msg for kw in _SDK_CORRUPTION_KEYWORDS)


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
    def get_realtime_universe(self) -> dict[str, str]: ...
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

    @property
    def calendar_set(self) -> frozenset: ...

    @property
    def last_login_error(self) -> dict | None: ...
    @property
    def reconnect_attempts(self) -> int: ...


class GatewayError(Exception):
    """Gateway 层所有异常的基类。"""
    pass


class GatewayNotReadyError(GatewayError):
    """SDK 未登录或未初始化，映射为 HTTP 503。"""
    pass


class GatewayQueryError(GatewayError):
    """SDK 查询失败或周期不支持，映射为 HTTP 502。"""
    pass

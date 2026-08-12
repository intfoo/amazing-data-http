"""AmazingData SDK 网关包：AmazingDataGateway 由各职责 Mixin 组合而成。

拆分自原 app/gateway.py（1018 行超单文件可维护阈值），行为零变化。
公共导入面（from app.gateway import ...）与本模块 re-export 保持一致。
"""

import threading

from app.config import Config
from app.gateway.base import (
    PERIOD_MAP,
    Gateway,
    GatewayError,
    GatewayNotReadyError,
    GatewayQueryError,
    _is_connection_error,
    _is_sdk_corruption,
)
from app.gateway.paths import (
    resolve_adj_factor_local_path,
    resolve_fund_local_path,
)
from app.gateway.query_basedata import QueryBaseDataMixin
from app.gateway.query_market import QueryMarketMixin
from app.gateway.resilience import ResilienceMixin
from app.gateway.session import SessionMixin
from app.gateway.subscription import SubscriptionMixin
from app.gateway.tgw_events import TgwEventMixin

__all__ = [
    "AmazingDataGateway",
    "Gateway",
    "GatewayError",
    "GatewayNotReadyError",
    "GatewayQueryError",
    "PERIOD_MAP",
    "_is_connection_error",
    "_is_sdk_corruption",
]


class AmazingDataGateway(
    SessionMixin,
    TgwEventMixin,
    ResilienceMixin,
    QueryMarketMixin,
    QueryBaseDataMixin,
    SubscriptionMixin,
):
    """AmazingData SDK 的真实封装实现（组合各职责 Mixin，原 gateway.py 文档注释随行）。

    管理进程级 SDK 会话：启动时 login + 创建 MarketData，
    后续请求复用同一会话，避免重复登录。SDK 对象非线程安全，
    所有操作通过 _lock 串行化（_sdk_lock 带 30s 竞争超时）。
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
        self._adj_factor_local_path = resolve_adj_factor_local_path(config.adj_factor_local_path)
        self._info_data = None  # ad.InfoData 实例（供 get_fund_share/get_fund_nav）
        self._fund_local_path = resolve_fund_local_path(config.fund_local_path)
        # 主动重连状态（tgw 断线回调触发，后台线程执行）
        self._reconnect_lock = threading.Lock()
        self._reconnect_in_progress = False
        self._last_reconnect_attempt = 0.0
        self._last_disconnect_log: dict = {"msg": None, "ts": 0.0}
        self._last_noise_log: dict = {"msg": None, "ts": 0.0}  # tgw 噪音 dedup（独立槽位）
        self._reconnect_failures = 0    # 连续重连失败计数（指数退避用）
        self._reconnect_attempts = 0    # 累计重连尝试（/health 诊断）
        # 登录诊断（last_login_error / spi probe / 事件缓冲）
        self._last_login_error: dict | None = None
        self._last_login_spi = None     # set_cfg probe 捕获的 log_spi
        self._login_events: list[dict] = []  # 登录窗口事件环形缓冲（≤20 条）
        self._login_in_progress = False

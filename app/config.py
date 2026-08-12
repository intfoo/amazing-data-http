"""服务配置：从环境变量读取 AmazingData SDK 凭据和 HTTP 监听参数。

凭据只通过环境变量注入（Docker env_file 或 secrets），不写入源码或镜像层。
配置缺失时进程仍可启动（/health 返回 503），以便 Docker 日志暴露诊断信息。
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("amazingdata.config")


def _env_int(name: str, default: int) -> int:
    """读取 int 型环境变量。缺失/空串用默认值；非法值 warning 后回退默认值（不崩溃）。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法整数，使用默认值 %d", name, raw, default)
        return default


@dataclass(frozen=True)
class Config:
    """不可变配置对象。frozen=True 防止运行时意外篡改。"""

    username: str        # AmazingData 账号
    password: str        # AmazingData 密码（不记录到日志）
    ip: str              # AmazingData/tgw 服务器 IP
    port: int            # AmazingData/tgw 服务器端口
    http_host: str = "0.0.0.0"  # HTTP 监听地址，默认全网卡
    http_port: int = 3021       # HTTP 监听端口
    sdk_max_concurrent: int = 2  # SDK 最大并发调用数（SDK 调用全局串行，此值只决定排队深度），超出返回 503
    adj_factor_local_path: str = ""  # SDK get_adj_factor 的 local_path 参数，必须为绝对路径
    adj_factor_is_local: bool = False  # SDK get_adj_factor 的 is_local：False=每次远程取最新，True=本地优先无则远程
    fund_local_path: str = ""    # SDK get_fund_share/get_fund_nav 的 local_path，必须为绝对路径
    fund_is_local: bool = False  # SDK is_local：False=每次远程取最新，True=本地优先无则远程
    auth_token: str = ""  # Bearer token；AUTH_REQUIRED=true 时客户端必须携带
    auth_required: bool = False  # 字段默认 False（测试便利）；env 默认 "true"（生产安全）
    subscription_open: str = "09:00"       # 订阅窗口开始 HH:MM
    subscription_close: str = "15:20"      # 订阅窗口结束 HH:MM
    stale_threshold_sec: int = 90          # watchdog 失活阈值（秒）
    watchdog_interval_sec: int = 60        # watchdog 检查间隔（秒）
    calendar_fallback_weekday: bool = True  # 日历不含今天时用 weekday 兜底（周一~周五视为交易日）
    reconnect_max_interval_sec: int = 300  # tgw 主动重连退避上限（秒）
    stale_max_age_sec: int = 300           # /realtime 订阅缓存 stale 上限（秒），超过走 fallback
    etf_flow_cache_ttl_sec: int = 300  # /etf/net_inflow 结果缓存 TTL（秒），份额 T+1 更新无 freshness 风险

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量构造 Config。缺失值给空字符串/0 作为安全默认。"""
        return cls(
            username=os.environ.get("AMAZINGDATA_USERNAME", ""),
            password=os.environ.get("AMAZINGDATA_PASSWORD", ""),
            ip=os.environ.get("AMAZINGDATA_HOST", ""),
            port=_env_int("AMAZINGDATA_PORT", 0),
            http_host=os.environ.get("HTTP_HOST", "0.0.0.0") or "0.0.0.0",
            http_port=_env_int("HTTP_PORT", 3021),
            sdk_max_concurrent=_env_int("SDK_MAX_CONCURRENT", 2),
            adj_factor_local_path=os.environ.get("ADJ_FACTOR_LOCAL_PATH", "") or "",
            adj_factor_is_local=os.environ.get("ADJ_FACTOR_IS_LOCAL", "false").lower()
            in ("1", "true", "yes", "on"),
            fund_local_path=os.environ.get("FUND_LOCAL_PATH", "") or "",
            fund_is_local=os.environ.get("FUND_IS_LOCAL", "false").lower()
            in ("1", "true", "yes", "on"),
            auth_token=os.environ.get("AUTH_TOKEN", ""),
            auth_required=os.environ.get("AUTH_REQUIRED", "true").lower()
            in ("1", "true", "yes", "on"),
            subscription_open=os.environ.get("SUBSCRIPTION_OPEN", "09:00") or "09:00",
            subscription_close=os.environ.get("SUBSCRIPTION_CLOSE", "15:20") or "15:20",
            stale_threshold_sec=_env_int("STALE_THRESHOLD_SEC", 90),
            watchdog_interval_sec=_env_int("WATCHDOG_INTERVAL_SEC", 60),
            calendar_fallback_weekday=os.environ.get("CALENDAR_FALLBACK_WEEKDAY", "true").lower()
            in ("1", "true", "yes", "on"),
            reconnect_max_interval_sec=_env_int("RECONNECT_MAX_INTERVAL_SEC", 300),
            stale_max_age_sec=_env_int("STALE_MAX_AGE_SEC", 300),
            etf_flow_cache_ttl_sec=_env_int("ETF_FLOW_CACHE_TTL_SEC", 300),
        )

    def is_configured(self) -> bool:
        """四项必填凭据是否全部非空。决定启动时是否尝试登录 SDK。"""
        return bool(self.username and self.password and self.ip and self.port)

    def is_auth_valid(self) -> bool:
        """返回 auth 配置是否有效。与 is_configured() 同风格（查询方法）。

        - auth_required=False：始终返回 True（认证关闭，token 被忽略）
        - auth_required=True：token 非空 + 长度>12 + 含字母和数字
        """
        if not self.auth_required:
            return True
        if not self.auth_token:
            return False
        if len(self.auth_token) <= 12:
            return False
        has_alpha = any(c.isalpha() for c in self.auth_token)
        has_digit = any(c.isdigit() for c in self.auth_token)
        return bool(has_alpha and has_digit)

    def validate_auth(self) -> None:
        """启动时校验 auth 配置。失败抛 ValueError，由 lifespan 捕获后进程退出。
        内部委托给 is_auth_valid()，保证与 HealthService.status() 同口径。
        """
        if self.is_auth_valid():
            return
        if not self.auth_token:
            raise ValueError("AUTH_TOKEN is required when AUTH_REQUIRED=true")
        if len(self.auth_token) <= 12:
            raise ValueError(
                f"AUTH_TOKEN too short: {len(self.auth_token)} chars, need > 12"
            )
        raise ValueError("AUTH_TOKEN must contain both letters and digits")

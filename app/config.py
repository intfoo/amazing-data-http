"""服务配置：从环境变量读取 AmazingData SDK 凭据和 HTTP 监听参数。

凭据只通过环境变量注入（Docker env_file 或 secrets），不写入源码或镜像层。
配置缺失时进程仍可启动（/health 返回 503），以便 Docker 日志暴露诊断信息。
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """不可变配置对象。frozen=True 防止运行时意外篡改。"""

    username: str        # AmazingData 账号
    password: str        # AmazingData 密码（不记录到日志）
    ip: str              # AmazingData/tgw 服务器 IP
    port: int            # AmazingData/tgw 服务器端口
    http_host: str = "0.0.0.0"  # HTTP 监听地址，默认全网卡
    http_port: int = 3021       # HTTP 监听端口
    sdk_max_concurrent: int = 5  # SDK 最大并发调用数，超出返回 503
    adj_factor_local_path: str = ""  # SDK get_adj_factor 的 local_path 参数，必须为绝对路径
    auth_token: str = ""  # Bearer token；AUTH_REQUIRED=true 时客户端必须携带
    auth_required: bool = False  # 字段默认 False（测试便利）；env 默认 "true"（生产安全）
    subscription_open: str = "09:00"       # 订阅窗口开始 HH:MM
    subscription_close: str = "15:20"      # 订阅窗口结束 HH:MM
    stale_threshold_sec: int = 90          # watchdog 失活阈值（秒）
    watchdog_interval_sec: int = 60        # watchdog 检查间隔（秒）

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量构造 Config。缺失值给空字符串/0 作为安全默认。"""
        return cls(
            username=os.environ.get("AMAZINGDATA_USERNAME", ""),
            password=os.environ.get("AMAZINGDATA_PASSWORD", ""),
            ip=os.environ.get("AMAZINGDATA_HOST", ""),
            port=int(os.environ.get("AMAZINGDATA_PORT", "0") or "0"),
            http_host=os.environ.get("HTTP_HOST", "0.0.0.0") or "0.0.0.0",
            http_port=int(os.environ.get("HTTP_PORT", "3021") or "3021"),
            sdk_max_concurrent=int(os.environ.get("SDK_MAX_CONCURRENT", "5") or "5"),
            adj_factor_local_path=os.environ.get("ADJ_FACTOR_LOCAL_PATH", "") or "",
            auth_token=os.environ.get("AUTH_TOKEN", ""),
            auth_required=os.environ.get("AUTH_REQUIRED", "true").lower()
            in ("1", "true", "yes", "on"),
            subscription_open=os.environ.get("SUBSCRIPTION_OPEN", "09:00") or "09:00",
            subscription_close=os.environ.get("SUBSCRIPTION_CLOSE", "15:20") or "15:20",
            stale_threshold_sec=int(os.environ.get("STALE_THRESHOLD_SEC", "90") or "90"),
            watchdog_interval_sec=int(os.environ.get("WATCHDOG_INTERVAL_SEC", "60") or "60"),
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

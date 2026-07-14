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
        )

    def is_configured(self) -> bool:
        """四项必填凭据是否全部非空。决定启动时是否尝试登录 SDK。"""
        return bool(self.username and self.password and self.ip and self.port)

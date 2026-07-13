"""健康检查服务：综合配置完整性和 SDK 登录状态判断服务是否可用。

/health 路由返回 200（全部就绪）或 503（配置缺失/SDK 未登录），
响应体不含密码或连接凭据，可安全暴露给 Docker healthcheck。
"""

from app.gateway import Gateway


class HealthService:
    def __init__(self, config, gateway: Gateway):
        self._config = config
        self._gw = gateway

    def status(self) -> dict:
        """返回健康状态详情。status=ok 当且仅当配置完整且 SDK 已登录。"""
        ready = self._config.is_configured() and self._gw.is_ready()
        return {
            "status": "ok" if ready else "degraded",
            "sdk": "ready" if self._gw.is_ready() else "not_ready",
            "config": "complete" if self._config.is_configured() else "incomplete",
        }

    def is_ok(self) -> bool:
        """快捷判断：配置完整 + SDK 就绪。/health 据此返回 200 或 503。"""
        return self._config.is_configured() and self._gw.is_ready()

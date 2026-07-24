"""健康检查服务：综合配置完整性、SDK 登录状态和订阅存活状态判断服务是否可用。

/health 路由返回 200（全部就绪）或 503（配置缺失/SDK 未登录/窗口期内订阅失活），
响应体不含密码或连接凭据，可安全暴露给 Docker healthcheck。
"""

import datetime

from app.gateway import Gateway
from app.subscription_schedule import is_subscription_window


class HealthService:
    def __init__(self, config, gateway: Gateway, realtime_service=None):
        self._config = config
        self._gw = gateway
        self._realtime_svc = realtime_service

    def _realtime_detail(self) -> str:
        """计算 realtime_detail 状态。"""
        rt_svc = self._realtime_svc
        if not rt_svc:
            return "unavailable"
        if rt_svc.is_active():
            return "active"
        # inactive 分情况
        now = datetime.datetime.now()
        cal = self._gw.calendar
        if not cal or not is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
            calendar_fallback_weekday=self._config.calendar_fallback_weekday,
        ):
            return "inactive_offhours"
        # 窗口期内 inactive
        reason = rt_svc.deactivation_reason()
        if reason == "stale":
            return "inactive_stale"
        if reason == "error":
            return "inactive_error"
        if rt_svc.last_snapshot_ts() == 0:
            return "inactive_not_started"
        return "inactive_stale"  # 有过数据但无明确 reason，按 stale 处理

    def status(self) -> dict:
        """返回健康状态详情。"""
        ready = self._config.is_configured() and self._gw.is_ready()
        rt_detail = self._realtime_detail()
        rt = rt_detail == "active"
        if not self._config.auth_required:
            auth_state = "disabled"
        elif self._config.is_auth_valid():
            auth_state = "configured"
        else:
            auth_state = "misconfigured"
        return {
            "status": "ok" if (ready and self.is_ok()) else "degraded",
            "sdk": "ready" if self._gw.is_ready() else "not_ready",
            "config": "complete" if self._config.is_configured() else "incomplete",
            "realtime": "active" if rt else "inactive",
            "realtime_detail": rt_detail,
            "auth": auth_state,
        }

    def is_ok(self) -> bool:
        """快捷判断：配置完整 + SDK 就绪 + 窗口期内订阅活跃。/health 据此返回 200 或 503。"""
        if not (self._config.is_configured() and self._gw.is_ready()):
            return False
        now = datetime.datetime.now()
        cal = self._gw.calendar
        if cal and is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
            calendar_fallback_weekday=self._config.calendar_fallback_weekday,
        ):
            return self._realtime_svc.is_active() if self._realtime_svc else False
        return True

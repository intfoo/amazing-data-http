"""SessionMixin：SDK 登录/登出/就绪状态/交易日历刷新。"""

from __future__ import annotations

from app.gateway.base import GatewayNotReadyError, logger


class SessionMixin:
    """SDK 会话管理：login/logout/is_ready/calendar/refresh_calendar。"""

    def login(self) -> None:
        """线程安全的登录入口。"""
        with self._sdk_lock():
            self._do_login()

    def _do_login(self) -> None:
        """实际登录流程：import SDK → login → BaseData → get_calendar → MarketData。

        SDK import 延迟到此处（而非模块顶部），使服务在无 SDK 环境下也能启动，
        /health 能正常返回 503 诊断信息。

        连接泄漏防护：ad.login() 成功后若后续步骤（BaseData/MarketData）失败，
        必须调 _safe_logout 释放已建立的 SDK 连接，否则 _ready 仍为 False，
        下次重 login 时不会先 logout（因 if self._ready 为 False），旧连接泄漏。
        """
        try:
            import AmazingData as ad
        except ImportError as e:
            logger.error("AmazingData SDK 导入失败: %s", e)
            self._ready = False
            raise GatewayNotReadyError(f"SDK import failed: {e}") from e

        self._ad = ad
        sdk_logged_in = False  # 标记 ad.login 是否已成功，用于失败时回滚
        try:
            if self._ready:
                self._safe_logout()
            ad.login(
                username=self._config.username,
                password=self._config.password,
                host=self._config.ip,
                port=self._config.port,
            )
            sdk_logged_in = True
            base = ad.BaseData()
            self._base_data = base
            calendar = base.get_calendar()
            self._calendar = calendar
            self._market_data = ad.MarketData(calendar)
            self._info_data = ad.InfoData()
            self._ready = True
            logger.info("SDK 登录成功")
            self._install_tgw_event_logger()
        except Exception as e:
            # ad.login 已成功但后续步骤失败：必须 logout 释放连接，否则连接泄漏
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("SDK 登录失败: %s: %s", type(e).__name__, e)
            raise GatewayNotReadyError(f"login failed: {e}") from e

    def logout(self) -> None:
        """线程安全的登出入口。"""
        with self._sdk_lock():
            self._safe_logout()

    def _safe_logout(self) -> None:
        """登出并清理状态。登出异常被忽略（不影响后续重登录）。"""
        if self._ad is None:
            return
        try:
            self._ad.logout(username=self._config.username)
        except Exception as e:
            logger.warning("登出异常（已忽略）: %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None
        self._base_data = None
        self._info_data = None
        self._calendar = None

    def is_ready(self) -> bool:
        """SDK 是否已登录且 MarketData 已初始化。"""
        return self._ready

    @property
    def calendar(self) -> list[int] | None:
        """交易日历 list[int]（login 后可用，logout 后为 None）。"""
        return self._calendar

    def refresh_calendar(self) -> list[int]:
        """重新拉取交易日历并热更新到 MarketData.calendar 属性。

        根因背景：SDK query_kline 用 login 时快照的 calendar 本地过滤 date_list
        （market_data.pyc 字节码证实），日历不含查询日时 date_list 为空 →
        零网络请求 0.000s 静默返回 {}。长运行服务跨天后必须刷新日历。
        MarketData.calendar 是普通属性，直接赋值即热生效。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        with self._sdk_lock():
            calendar = self._base_data.get_calendar()
            self._calendar = calendar
            if self._market_data is not None:
                self._market_data.calendar = calendar
            logger.info(
                "交易日历已刷新: %d 天, 最新=%s",
                len(calendar), calendar[-1] if calendar else None,
            )
            return calendar

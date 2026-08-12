"""SessionMixin：SDK 登录/登出/就绪状态/交易日历刷新。"""

from __future__ import annotations

import time

from app.gateway.base import GatewayNotReadyError, logger


class SessionMixin:
    """SDK 会话管理：login/logout/is_ready/calendar/refresh_calendar。"""

    def login(self) -> None:
        """线程安全的登录入口。"""
        with self._sdk_lock():
            self._do_login()

    def _do_login(self) -> None:
        """实际登录流程：import SDK → 装事件钩子/probe → login → BaseData → calendar → MarketData。

        SystemExit 兜底：SDK tgw_login.login 失败路径是 print('login fail') + exit(0)，
        SystemExit 是 BaseException，except Exception 接不住，必须显式捕获，
        否则穿透 lifespan 杀进程（uvicorn startup failed → 容器重启死循环）。
        """
        try:
            import AmazingData as ad
        except ImportError as e:
            logger.error("AmazingData SDK 导入失败: %s", e)
            self._ready = False
            raise GatewayNotReadyError(f"SDK import failed: {e}") from e

        self._ad = ad
        # 钩子/probe 前置：import tgw 后 g_spi 即存在（interface.py 模块级创建），
        # login 前安装可捕获失败全程的 OnLog/OnLogon 事件（真实失败原因）。
        self._install_tgw_event_logger()
        self._install_login_spi_probe()
        sdk_logged_in = False
        self._login_events.clear()
        self._login_in_progress = True
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
            self._last_login_error = None
            logger.info("SDK 登录成功")
        except SystemExit as e:
            # SDK login 内部 exit(0) → 进程存活兜底
            self._build_last_login_error("sdk_exit", f"SDK login 内部 exit({e.code})")
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("SDK 登录失败: %s", self._last_login_error)
            raise GatewayNotReadyError(f"login failed: SDK exit({e.code})") from e
        except Exception as e:
            self._build_last_login_error("exception", f"{type(e).__name__}: {e}")
            if sdk_logged_in:
                self._safe_logout()
            self._ready = False
            logger.error("SDK 登录失败: %s", self._last_login_error)
            raise GatewayNotReadyError(f"login failed: {e}") from e
        finally:
            self._login_in_progress = False

    def logout(self) -> None:
        """线程安全的登出入口。shutdown 路径：清空 calendar。"""
        with self._sdk_lock():
            self._safe_logout(clear_calendar=True)

    def _safe_logout(self, clear_calendar: bool = False) -> None:
        """登出并清理状态。登出异常被忽略（不影响后续重登录）。

        clear_calendar=False（重连路径默认）：保留 calendar（纯日期数据，当天有效），
        供调度器在重连失败期间正确判定订阅窗口，避免误判"不在窗口"杀订阅清缓存。
        """
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
        if clear_calendar:
            self._calendar = None

    def _build_last_login_error(self, category: str, detail: str) -> None:
        """构建登录失败诊断：spi max_limitation 升级分类 + 登录窗口事件缓冲。"""
        spi = self._last_login_spi
        if spi is not None and getattr(spi, "max_limitation", False):
            category = "max_limitation"
        self._last_login_error = {
            "ts": time.time(),
            "category": category,
            "detail": detail,
            "events": list(self._login_events)[-20:],
        }

    @property
    def last_login_error(self) -> dict | None:
        """最近一次登录失败诊断 {ts, category, detail, events}。成功登录后为 None。"""
        return self._last_login_error

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

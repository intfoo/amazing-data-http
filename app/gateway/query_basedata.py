"""基础数据查询 Mixin：复权因子、基金份额、基金净值。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.gateway.base import (
    ADJ_FACTOR_TIMEOUT_SEC,
    GatewayNotReadyError,
    GatewayQueryError,
    _is_connection_error,
    _is_sdk_corruption,
    logger,
)


class QueryBaseDataMixin:
    """基础数据查询职责 Mixin：复权因子、基金份额、基金净值。"""

    def get_adj_factor(self, codes: list[str]) -> "pd.DataFrame":
        """获取单次复权因子（手册 3.5.2.6）。返回 SDK 原始 DataFrame（宽表：index=交易日期, columns=股票代码）。

        SDK 签名 get_adj_factor(code_list, local_path, is_local)，无日期参数。
        is_local 由 Config.adj_factor_is_local 控制（环境变量 ADJ_FACTOR_IS_LOCAL）：
        - False（默认）：每次从服务端取最新，仍会更新 local_path 缓存（手册注(2)）。每次 ~21s。
        - True：本地有缓存则读本地（<1s），本地无则远程取 + 写本地（首次 ~21s）。
          风险：本地缓存可能陈旧（adj_factor 除权事件一年几次，风险低但不为零）。
        local_path 在启动时由 _resolve_adj_factor_local_path 解析（配置优先，否则项目根 data 兜底，
        SDK 会自建 basedata/adj_factor/ 子目录）。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        is_local = self._config.adj_factor_is_local
        with self._sdk_lock():
            try:
                result = self._call_sdk_with_timeout(
                    lambda: self._base_data.get_adj_factor(
                        codes,
                        local_path=self._adj_factor_local_path,
                        is_local=is_local,
                    ),
                    ADJ_FACTOR_TIMEOUT_SEC,
                    "get_adj_factor",
                )
            except Exception as e:
                logger.error("get_adj_factor 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local, exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_adj_factor 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._call_sdk_with_timeout(
                            lambda: self._base_data.get_adj_factor(
                                codes,
                                local_path=self._adj_factor_local_path,
                                is_local=is_local,
                            ),
                            ADJ_FACTOR_TIMEOUT_SEC,
                            "get_adj_factor",
                        )
                        logger.info("get_adj_factor 重连后成功")
                    except Exception as e2:
                        logger.error("get_adj_factor 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_adj_factor failed after reconnect: {e2}") from e2
                else:
                    if _is_sdk_corruption(e):
                        logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                        try:
                            self._do_login()
                        except Exception as e3:
                            logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                    raise GatewayQueryError(f"get_adj_factor failed: {e}") from e
            # SDK 内部状态损坏时可能静默返回 None（如本地 HDF5 缓存损坏，
            # 下游 df[...] 直接 TypeError）。is_local=True 时回退远程重试一次。
            if result is None:
                if is_local:
                    logger.warning(
                        "get_adj_factor is_local=True 返回 None（本地缓存可能损坏），"
                        "回退 is_local=False 远程重试"
                    )
                    result = self._call_sdk_with_timeout(
                        lambda: self._base_data.get_adj_factor(
                            codes,
                            local_path=self._adj_factor_local_path,
                            is_local=False,
                        ),
                        ADJ_FACTOR_TIMEOUT_SEC,
                        "get_adj_factor",
                    )
                if result is None:
                    raise GatewayQueryError(
                        "get_adj_factor returned None（SDK 内部错误，"
                        "建议删除本地 adj_factor 缓存后重试）"
                    )
            return result

    def get_fund_share(
        self,
        codes: list[str],
        is_local: bool = False,
        begin_date: int | None = None,
        end_date: int | None = None,
    ) -> dict[str, "pd.DataFrame"]:
        """获取基金/ETF 份额历史时序，委托 InfoData.get_fund_share。

        模式对标 get_adj_factor：检查 _ready/_info_data → 加 _lock 串行化 → 委托 SDK → 连接错误重连。
        local_path 由 Gateway 内部从 Config.fund_local_path 读取（_resolve_fund_local_path 解析），
        is_local 由 Config.fund_is_local 控制（Protocol 签名保留 is_local 仅为接口契约明确性）。
        返回 dict[code, DataFrame]（DataFrame 含 FUND_SHARE, CHANGE_DATE 等列）。
        """
        if not self._ready or self._info_data is None:
            raise GatewayNotReadyError("gateway not ready")
        is_local = self._config.fund_is_local
        with self._sdk_lock():
            try:
                return self._info_data.get_fund_share(
                    codes,
                    local_path=self._fund_local_path,
                    is_local=is_local,
                    begin_date=begin_date,
                    end_date=end_date,
                )
            except Exception as e:
                logger.error("get_fund_share 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local,
                             exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_fund_share 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._info_data.get_fund_share(
                            codes,
                            local_path=self._fund_local_path,
                            is_local=is_local,
                            begin_date=begin_date,
                            end_date=end_date,
                        )
                        logger.info("get_fund_share 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_fund_share 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_fund_share failed after reconnect: {e2}") from e2
                if _is_sdk_corruption(e):
                    logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                raise GatewayQueryError(f"get_fund_share failed: {e}") from e

    def get_fund_nav(
        self,
        codes: list[str],
        is_local: bool = False,
        begin_date: int | None = None,
        end_date: int | None = None,
    ) -> dict[str, "pd.DataFrame"]:
        """获取基金/ETF 净值历史时序，委托 InfoData.get_fund_nav。

        模式同 get_fund_share：检查 _ready/_info_data → 加 _lock 串行化 → 委托 SDK → 连接错误重连。
        local_path 由 Gateway 内部从 Config.fund_local_path 读取，is_local 由 Config.fund_is_local 控制。
        返回 dict[code, DataFrame]（DataFrame 含 UNIT_NAV, PRICE_DATE 等列）。
        """
        if not self._ready or self._info_data is None:
            raise GatewayNotReadyError("gateway not ready")
        is_local = self._config.fund_is_local
        with self._sdk_lock():
            try:
                return self._info_data.get_fund_nav(
                    codes,
                    local_path=self._fund_local_path,
                    is_local=is_local,
                    begin_date=begin_date,
                    end_date=end_date,
                )
            except Exception as e:
                logger.error("get_fund_nav 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local,
                             exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_fund_nav 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._info_data.get_fund_nav(
                            codes,
                            local_path=self._fund_local_path,
                            is_local=is_local,
                            begin_date=begin_date,
                            end_date=end_date,
                        )
                        logger.info("get_fund_nav 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_fund_nav 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_fund_nav failed after reconnect: {e2}") from e2
                if _is_sdk_corruption(e):
                    logger.warning("检测到 SDK 内部状态损坏，重建会话释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                raise GatewayQueryError(f"get_fund_nav failed: {e}") from e

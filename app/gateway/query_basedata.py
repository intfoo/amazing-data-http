"""基础数据查询 Mixin：复权因子、基金份额、基金净值。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.gateway.base import (
    GatewayNotReadyError,
    GatewayQueryError,
    _is_connection_error,
    _is_sdk_corruption,
    logger,
)


def _is_local_cache_error(e: BaseException) -> bool:
    """SDK 本地 HDF5 缓存损坏特征（is_local=True 读缓存时抛出）。

    2026-08-28 线上事故：adj_factor.h5 半写截断，tables 抛 HDF5ExtError
    （"incorrect metadata checksum" / "Unable to open/create file"）。
    用鸭子类型判定（type 名或消息含 HDF5），避免模块级 import tables。
    """
    msg = str(e)
    return (
        "HDF5" in type(e).__name__
        or "HDF5" in msg
        or "Unable to open/create file" in msg
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
                    self._config.sdk_call_timeout_sec,
                    "get_adj_factor",
                )
            except Exception as e:
                logger.error("get_adj_factor 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local, exc_info=True)
                if is_local and _is_local_cache_error(e):
                    # 本地 HDF5 缓存损坏（2026-08-28 线上事故）：隔离留证 + 远程重试一次。
                    # 注意：不能用少量 code 暖缓存——SDK is_local 增量合并不生效，
                    # 缓存只含部分 code 时后续请求会静默错数据，因此远程重试用本次完整 codes。
                    logger.warning(
                        "adj_factor 本地缓存损坏（HDF5），隔离后回退 is_local=False 远程重试"
                    )
                    self._quarantine_adj_factor_cache(reason=type(e).__name__)
                    try:
                        result = self._call_sdk_with_timeout(
                            lambda: self._base_data.get_adj_factor(
                                codes,
                                local_path=self._adj_factor_local_path,
                                is_local=False,
                            ),
                            self._config.sdk_call_timeout_sec,
                            "get_adj_factor",
                        )
                        logger.info("get_adj_factor 缓存隔离后远程重试成功")
                    except Exception as e2:
                        logger.error("get_adj_factor 缓存隔离后远程重试仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(
                            f"get_adj_factor failed after cache quarantine: {e2}"
                        ) from e2
                elif _is_connection_error(e):
                    logger.warning("get_adj_factor 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._call_sdk_with_timeout(
                            lambda: self._base_data.get_adj_factor(
                                codes,
                                local_path=self._adj_factor_local_path,
                                is_local=is_local,
                            ),
                            self._config.sdk_call_timeout_sec,
                            "get_adj_factor",
                        )
                        logger.info("get_adj_factor 重连后成功")
                    except Exception as e2:
                        logger.error("get_adj_factor 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_adj_factor failed after reconnect: {e2}") from e2
                else:
                    if _is_sdk_corruption(e):
                        logger.warning("检测到 SDK 内部状态损坏，重建会话（含换锁）释放 SDK 内部锁: %s", e)
                        try:
                            self._do_login()
                        except Exception as e3:
                            logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                    raise GatewayQueryError(f"get_adj_factor failed: {e}") from e
            # SDK 内部状态损坏时可能静默返回 None（如本地 HDF5 缓存损坏，
            # 下游 df[...] 直接 TypeError）。is_local=True 时隔离缓存 + 回退远程重试一次。
            if result is None:
                if is_local:
                    logger.warning(
                        "get_adj_factor is_local=True 返回 None（本地缓存可能损坏），"
                        "隔离缓存并回退 is_local=False 远程重试"
                    )
                    self._quarantine_adj_factor_cache(reason="returned None")
                    result = self._call_sdk_with_timeout(
                        lambda: self._base_data.get_adj_factor(
                            codes,
                            local_path=self._adj_factor_local_path,
                            is_local=False,
                        ),
                        self._config.sdk_call_timeout_sec,
                        "get_adj_factor",
                    )
                if result is None:
                    raise GatewayQueryError(
                        "get_adj_factor returned None（SDK 内部错误，"
                        "建议删除本地 adj_factor 缓存后重试）"
                    )
            return result

    def _quarantine_adj_factor_cache(self, reason: str) -> None:
        """隔离损坏的 adj_factor 本地 HDF5 缓存：重命名留证（不删除），下次 SDK 远程重建。

        缓存路径 = local_path + 'basedata/adj_factor/adj_factor.h5'（SDK 字符串拼接，
        local_path 末尾已强制带分隔符，Path 可正常处理）。文件不存在时 no-op。
        重命名失败只记日志不抛出——自愈路径不能让隔离失败阻断远程重试。
        """
        cache_dir = Path(self._adj_factor_local_path) / "basedata" / "adj_factor"
        cache_file = cache_dir / "adj_factor.h5"
        if not cache_file.exists():
            return
        quarantined = cache_dir / f"adj_factor.h5.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
        try:
            cache_file.rename(quarantined)
            logger.warning(
                "adj_factor 本地缓存已隔离: %s -> %s（原因: %s）",
                cache_file, quarantined.name, reason,
            )
        except OSError as e:
            logger.error("adj_factor 缓存隔离失败: %s: %s", cache_file, e)

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
                return self._call_sdk_with_timeout(
                    lambda: self._info_data.get_fund_share(
                        codes,
                        local_path=self._fund_local_path,
                        is_local=is_local,
                        begin_date=begin_date,
                        end_date=end_date,
                    ),
                    self._config.sdk_call_timeout_sec,
                    "get_fund_share",
                )
            except Exception as e:
                logger.error("get_fund_share 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local,
                             exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_fund_share 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._call_sdk_with_timeout(
                            lambda: self._info_data.get_fund_share(
                                codes,
                                local_path=self._fund_local_path,
                                is_local=is_local,
                                begin_date=begin_date,
                                end_date=end_date,
                            ),
                            self._config.sdk_call_timeout_sec,
                            "get_fund_share",
                        )
                        logger.info("get_fund_share 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_fund_share 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_fund_share failed after reconnect: {e2}") from e2
                if _is_sdk_corruption(e):
                    logger.warning("检测到 SDK 内部状态损坏，重建会话（含换锁）释放 SDK 内部锁: %s", e)
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
                return self._call_sdk_with_timeout(
                    lambda: self._info_data.get_fund_nav(
                        codes,
                        local_path=self._fund_local_path,
                        is_local=is_local,
                        begin_date=begin_date,
                        end_date=end_date,
                    ),
                    self._config.sdk_call_timeout_sec,
                    "get_fund_nav",
                )
            except Exception as e:
                logger.error("get_fund_nav 失败: %s: %s (codes=%d, is_local=%s)",
                             type(e).__name__, e, len(codes), is_local,
                             exc_info=True)
                if _is_connection_error(e):
                    logger.warning("get_fund_nav 连接错误，尝试重连: %s", e)
                    try:
                        self._do_login()
                        result = self._call_sdk_with_timeout(
                            lambda: self._info_data.get_fund_nav(
                                codes,
                                local_path=self._fund_local_path,
                                is_local=is_local,
                                begin_date=begin_date,
                                end_date=end_date,
                            ),
                            self._config.sdk_call_timeout_sec,
                            "get_fund_nav",
                        )
                        logger.info("get_fund_nav 重连后成功")
                        return result
                    except Exception as e2:
                        logger.error("get_fund_nav 重连后仍失败: %s: %s",
                                     type(e2).__name__, e2, exc_info=True)
                        raise GatewayQueryError(f"get_fund_nav failed after reconnect: {e2}") from e2
                if _is_sdk_corruption(e):
                    logger.warning("检测到 SDK 内部状态损坏，重建会话（含换锁）释放 SDK 内部锁: %s", e)
                    try:
                        self._do_login()
                    except Exception as e3:
                        logger.error("SDK 会话重建失败: %s: %s", type(e3).__name__, e3)
                raise GatewayQueryError(f"get_fund_nav failed: {e}") from e

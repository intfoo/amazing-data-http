"""FastAPI HTTP 应用：路由、错误处理、启动登录。

暴露两个端点：
- POST /daily  —— 日 K 查询（主项目自定义数据源协议）
- GET  /health —— 健康检查（Docker healthcheck + 诊断）

启动时自动登录 SDK；登录失败进程仍可启动，/health 返回 503。
所有错误统一为 {"error": {"code", "message", "request_id"}} 格式。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.config import Config
from app.errors import (
    AppError, INTERNAL_ERROR, INVALID_REQUEST, REALTIME_SUBSCRIPTION_FAILED,
    RequestIdMiddleware, SDK_NOT_READY, SDK_QUERY_FAILED, SERIALIZATION_FAILED, get_request_id,
)
from app.gateway import Gateway, GatewayNotReadyError, GatewayQueryError, AmazingDataGateway
from app.health import HealthService
from app.kline_service import KlineService
from app.realtime_service import RealtimeService

# 配置 amazingdata 命名空间日志：带时间戳，独立于 uvicorn 默认日志配置。
# propagate=False 防止 uvicorn 启动重配 root 后重复输出；
# 幂等判断 handlers 避免热重载或多次 import 重复添加。
_ad_root = logging.getLogger("amazingdata")
if not _ad_root.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _ad_root.addHandler(_h)
    _ad_root.setLevel(logging.INFO)
    _ad_root.propagate = False

logger = logging.getLogger("amazingdata.http")


def _align_uvicorn_log_format() -> None:
    """给 uvicorn 的 access/error logger 加时间戳前缀，与 amazingdata 日志对齐。

    uvicorn 默认 formatter 用 levelprefix（INFO→"INFO:"），不含时间。
    此处在 startup（uvicorn log_config 已 apply 之后）替换为带 %(asctime)s 的同类
    formatter，保留 levelprefix/client_addr/request_line 等 uvicorn 自定义字段。
    延迟 import uvicorn 避免无 uvicorn 环境（如纯 pytest）import 失败。
    """
    try:
        from uvicorn.logging import AccessFormatter, DefaultFormatter
    except ImportError:
        return
    datefmt = "%Y-%m-%d %H:%M:%S"
    # uvicorn.access：访问日志，格式同默认但加时间前缀
    for h in logging.getLogger("uvicorn.access").handlers:
        if isinstance(h.formatter, AccessFormatter):
            h.setFormatter(AccessFormatter(
                fmt='%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                datefmt=datefmt,
            ))
    # uvicorn：启动/错误日志
    for h in logging.getLogger("uvicorn").handlers:
        if isinstance(h.formatter, DefaultFormatter):
            h.setFormatter(DefaultFormatter(
                fmt="%(asctime)s %(levelprefix)s %(name)s - %(message)s",
                datefmt=datefmt,
            ))


class DailyRequest(BaseModel):
    """POST /daily 请求体。字段名与主项目自定义数据源协议一致。"""

    symbols: list[str]              # 股票代码列表，如 ["000001.SZ", "600000.SH"]
    start_time: str | None = None   # 开始日期，YYYY-MM-DD 或 ISO datetime（如 2024-01-01T00:00:00）；可选
    end_time: str | None = None     # 结束日期，同上；可选

    @field_validator("symbols")
    @classmethod
    def symbols_nonempty(cls, v):
        """symbols 必须是非空数组，Pydantic 校验失败自动返回 422。"""
        if not v or len(v) == 0:
            raise ValueError("symbols must be a non-empty array")
        return v


MINUTE_PERIODS = {"min1", "min3", "min5", "min10", "min15", "min30", "min60", "min120"}


class MinuteRequest(BaseModel):
    """POST /minute 请求体。period 可选，默认 min1。"""

    symbols: list[str]
    period: str | None = None
    start_time: str | None = None
    end_time: str | None = None

    @field_validator("symbols")
    @classmethod
    def symbols_nonempty(cls, v):
        if not v or len(v) == 0:
            raise ValueError("symbols must be a non-empty array")
        return v


def create_app(config: Config | None = None, gateway: Gateway | None = None) -> FastAPI:
    """创建 FastAPI 应用实例。

    config/gateway 可选注入：测试时传 FakeGateway，生产时默认从环境变量
    创建 Config + AmazingDataGateway。模块级 app = create_app() 供 uvicorn 直接引用。
    """
    app = FastAPI(title="AmazingData HTTP Adapter")
    app.add_middleware(RequestIdMiddleware)

    if config is None:
        config = Config.from_env()
    if gateway is None:
        gateway = AmazingDataGateway(config)

    kline_service = KlineService(gateway)
    realtime_service = RealtimeService(gateway)
    health_service = HealthService(config, gateway, realtime_service)

    app.state.config = config
    app.state.gateway = gateway
    app.state.kline_service = kline_service
    app.state.realtime_service = realtime_service
    app.state.health_service = health_service

    @app.on_event("startup")
    async def startup_login():
        """启动时尝试登录 SDK。配置缺失则跳过，/health 将返回 503。

        同时重配 uvicorn 日志 formatter 加时间戳，与 amazingdata 日志格式对齐
        （uvicorn 在触发 startup 前已完成自身 log_config，此时改 formatter 即生效）。
        """
        _align_uvicorn_log_format()
        if config.is_configured():
            try:
                gateway.login()
                logger.info("gateway login succeeded on startup")
                try:
                    code_list = gateway.get_code_list(security_type="EXTRA_STOCK_A")
                    gateway.start_snapshot_subscription(
                        code_list,
                        on_data=realtime_service.on_snapshot,
                        on_error=realtime_service.on_subscription_error,
                    )
                    realtime_service.set_active(True)
                    logger.info("realtime subscription started: %d symbols", len(code_list))
                except Exception as e:
                    logger.error("realtime subscription start failed: %s: %s", type(e).__name__, e)
            except Exception as e:
                # 登录失败不阻止启动，使 /health 能暴露诊断
                logger.error("gateway login failed on startup: %s: %s", type(e).__name__, e)
        else:
            logger.warning("config incomplete, skipping startup login")

    @app.on_event("shutdown")
    async def shutdown_logout():
        """进程退出时登出 SDK，释放服务端连接。

        docker stop / Ctrl+C 发 SIGTERM，uvicorn 优雅退出触发 shutdown 事件，
        此时调 gateway.logout() 释放 SDK 连接，避免服务端连接累积超限
        （TGW 报 "Connections of this user exceed the max limitation"）。
        """
        try:
            gateway.stop_subscription()
        except Exception as e:
            logger.warning("stop subscription on shutdown: %s: %s", type(e).__name__, e)
        try:
            gateway.logout()
            logger.info("gateway logout on shutdown")
        except Exception as e:
            logger.warning("gateway logout failed on shutdown: %s: %s", type(e).__name__, e)

    @app.get("/health")
    async def health():
        """健康检查。200=全部就绪，503=配置缺失或 SDK 未登录。"""
        hs: HealthService = app.state.health_service
        status = hs.status()
        code = 200 if hs.is_ok() else 503
        return JSONResponse(status_code=code, content=status)

    @app.post("/daily")
    async def daily(req: DailyRequest, request: Request):
        """日 K 查询。返回 {"data": [...]}，空结果也是 200 + {"data": []}。

        start_time / end_time 可选；未传时由 SDK 使用默认区间。
        日期格式校验与 start<=end 校验在 KlineService 内完成，ValueError 转 422。
        """
        logger.info("request_id=%s /daily symbols=%d %s..%s",
                    get_request_id(request), len(req.symbols),
                    req.start_time or "(default)", req.end_time or "(default)")
        try:
            data = kline_service.query(req.symbols, req.start_time, req.end_time)
            return {"data": data}
        except AppError:
            raise
        except ValueError as e:
            # to_sdk_date 校验失败
            raise AppError(INVALID_REQUEST, str(e), 422)
        except GatewayNotReadyError as e:
            raise AppError(SDK_NOT_READY, str(e), 503)
        except GatewayQueryError as e:
            raise AppError(SDK_QUERY_FAILED, str(e), 502)
        except (TypeError, OverflowError) as e:
            if "serialize" in str(e).lower() or "json" in str(e).lower():
                raise AppError(SERIALIZATION_FAILED, str(e), 502)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        except Exception as e:
            logger.error("unhandled error: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)

    @app.post("/minute")
    async def minute(req: MinuteRequest, request: Request):
        """分钟K查询。period 可选（默认 min1），白名单 min1~min120。返回 {"data": [...]}。"""
        period = req.period or "min1"
        if period not in MINUTE_PERIODS:
            raise AppError(INVALID_REQUEST, f"unsupported period: {period}", 422)
        logger.info("request_id=%s /minute symbols=%d period=%s %s..%s",
                    get_request_id(request), len(req.symbols), period,
                    req.start_time or "(default)", req.end_time or "(default)")
        try:
            data = kline_service.query(req.symbols, req.start_time, req.end_time, period=period)
            return {"data": data}
        except AppError:
            raise
        except ValueError as e:
            raise AppError(INVALID_REQUEST, str(e), 422)
        except GatewayNotReadyError as e:
            raise AppError(SDK_NOT_READY, str(e), 503)
        except GatewayQueryError as e:
            raise AppError(SDK_QUERY_FAILED, str(e), 502)
        except (TypeError, OverflowError) as e:
            if "serialize" in str(e).lower() or "json" in str(e).lower():
                raise AppError(SERIALIZATION_FAILED, str(e), 502)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        except Exception as e:
            logger.error("unhandled error: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)

    @app.get("/realtime")
    async def realtime(request: Request, symbols: str | None = None):
        """实时行情快照。可选 symbols 过滤，不传返回全市场。订阅未就绪返回 503。

        symbols 为逗号分隔的代码字符串（如 ?symbols=000001.SZ,600000.SH），
        从全市场缓存中过滤返回；不传则返回全市场。
        """
        logger.info("request_id=%s /realtime symbols=%s", get_request_id(request), symbols or "(all)")
        if not realtime_service.is_active():
            raise AppError(REALTIME_SUBSCRIPTION_FAILED, "realtime subscription not active", 503)
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
        data = realtime_service.snapshot(sym_list)
        return {"data": data}

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        """全局错误处理器：统一格式为 {"error": {"code", "message", "request_id"}}。"""
        request_id = get_request_id(request)
        logger.error("request_id=%s code=%s msg=%s", request_id, exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "request_id": request_id}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """请求体校验失败（422）：记录详细错误与原始 body，返回统一错误信封。"""
        request_id = get_request_id(request)
        errors = exc.errors()
        logger.warning("request_id=%s validation failed: errors=%s body=%s",
                       request_id, errors, repr(exc.body)[:500])
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": INVALID_REQUEST,
                "message": "请求体校验失败",
                "errors": jsonable_encoder(errors),
                "request_id": request_id,
            }},
        )

    return app


# 模块级 app 实例，供 uvicorn app.http_app:app 直接引用
app = create_app()

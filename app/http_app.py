"""FastAPI HTTP 应用：路由、错误处理、启动登录。

暴露两个端点：
- POST /daily  —— 日 K 查询（主项目自定义数据源协议）
- GET  /health —— 健康检查（Docker healthcheck + 诊断）

启动时自动登录 SDK；登录失败进程仍可启动，/health 返回 503。
所有错误统一为 {"error": {"code", "message", "request_id"}} 格式。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.config import Config
from app.errors import (
    AppError, INTERNAL_ERROR, INVALID_REQUEST, RequestIdMiddleware,
    SDK_NOT_READY, SDK_QUERY_FAILED, SERIALIZATION_FAILED, get_request_id,
)
from app.gateway import Gateway, GatewayNotReadyError, GatewayQueryError, AmazingDataGateway
from app.health import HealthService
from app.kline_service import KlineService

logger = logging.getLogger("amazingdata.http")


class DailyRequest(BaseModel):
    """POST /daily 请求体。字段名与主项目自定义数据源协议一致。"""

    symbols: list[str]     # 股票代码列表，如 ["000001.SZ", "600000.SH"]
    start_time: str        # 开始日期，YYYY-MM-DD
    end_time: str          # 结束日期，YYYY-MM-DD

    @field_validator("symbols")
    @classmethod
    def symbols_nonempty(cls, v):
        """symbols 必须是非空数组，Pydantic 校验失败自动返回 422。"""
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
    health_service = HealthService(config, gateway)

    app.state.config = config
    app.state.gateway = gateway
    app.state.kline_service = kline_service
    app.state.health_service = health_service

    @app.on_event("startup")
    async def startup_login():
        """启动时尝试登录 SDK。配置缺失则跳过，/health 将返回 503。"""
        if config.is_configured():
            try:
                gateway.login()
                logger.info("gateway login succeeded on startup")
            except Exception as e:
                # 登录失败不阻止启动，使 /health 能暴露诊断
                logger.error("gateway login failed on startup: %s: %s", type(e).__name__, e)
        else:
            logger.warning("config incomplete, skipping startup login")

    @app.get("/health")
    async def health():
        """健康检查。200=全部就绪，503=配置缺失或 SDK 未登录。"""
        hs: HealthService = app.state.health_service
        status = hs.status()
        code = 200 if hs.is_ok() else 503
        return JSONResponse(status_code=code, content=status)

    @app.post("/daily")
    async def daily(req: DailyRequest, request: Request):
        """日 K 查询。返回 {"data": [...]}，空结果也是 200 + {"data": []}。"""
        try:
            # ISO 日期字符串比较等价于日期比较（YYYY-MM-DD 格式天然有序）
            if req.start_time > req.end_time:
                raise AppError(INVALID_REQUEST, "start_time must not be later than end_time", 422)
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

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        """全局错误处理器：统一格式为 {"error": {"code", "message", "request_id"}}。"""
        request_id = get_request_id(request)
        logger.error("request_id=%s code=%s msg=%s", request_id, exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "request_id": request_id}},
        )

    return app


# 模块级 app 实例，供 uvicorn app.http_app:app 直接引用
app = create_app()

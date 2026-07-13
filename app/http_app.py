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
    symbols: list[str]
    start_time: str
    end_time: str

    @field_validator("symbols")
    @classmethod
    def symbols_nonempty(cls, v):
        if not v or len(v) == 0:
            raise ValueError("symbols must be a non-empty array")
        return v


def create_app(config: Config | None = None, gateway: Gateway | None = None) -> FastAPI:
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
        if config.is_configured():
            try:
                gateway.login()
                logger.info("gateway login succeeded on startup")
            except Exception as e:
                logger.error("gateway login failed on startup: %s: %s", type(e).__name__, e)
        else:
            logger.warning("config incomplete, skipping startup login")

    @app.get("/health")
    async def health():
        hs: HealthService = app.state.health_service
        status = hs.status()
        code = 200 if hs.is_ok() else 503
        return JSONResponse(status_code=code, content=status)

    @app.post("/daily")
    async def daily(req: DailyRequest, request: Request):
        try:
            if req.start_time > req.end_time:
                raise AppError(INVALID_REQUEST, "start_time must not be later than end_time", 422)
            data = kline_service.query(req.symbols, req.start_time, req.end_time)
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

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        request_id = get_request_id(request)
        logger.error("request_id=%s code=%s msg=%s", request_id, exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "request_id": request_id}},
        )

    return app


app = create_app()

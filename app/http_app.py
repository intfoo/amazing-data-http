"""FastAPI HTTP 应用：路由、错误处理、启动登录。

暴露两个端点：
- POST /daily  —— 日 K 查询（主项目自定义数据源协议）
- GET  /health —— 健康检查（Docker healthcheck + 诊断）

启动时自动登录 SDK；登录失败进程仍可启动，/health 返回 503。
所有错误统一为 {"error": {"code", "message", "request_id"}} 格式。
"""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.config import Config
from app.errors import (
    AppError, INTERNAL_ERROR, INVALID_REQUEST, REALTIME_SUBSCRIPTION_FAILED,
    RequestIdMiddleware, SDK_NOT_READY, SDK_QUERY_FAILED, SERIALIZATION_FAILED,
    SERVICE_BUSY, get_request_id,
)
from app.auth import AuthMiddleware
from app.gateway import Gateway, GatewayNotReadyError, GatewayQueryError, AmazingDataGateway
from app.health import HealthService
from app.adj_factor_service import AdjFactorService
from app.etf_flow_service import EtfFlowService
from app.kline_service import KlineService, MINUTE_PERIODS
from app.realtime_service import RealtimeService
from app.subscription_scheduler import SubscriptionScheduler

class _ShortNameFormatter(logging.Formatter):
    """显示 logger 名末段（去 amazingdata. 前缀）：[amazingdata.http] → [http]。"""

    def format(self, record):
        record.short_name = record.name.split(".")[-1]
        return super().format(record)


# 配置 amazingdata 命名空间日志：带时间戳，独立于 uvicorn 默认日志配置。
# propagate=False 防止 uvicorn 启动重配 root 后重复输出；
# 幂等判断 handlers 避免热重载或多次 import 重复添加。
_ad_root = logging.getLogger("amazingdata")
if not _ad_root.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(_ShortNameFormatter(
        "%(asctime)s %(levelname)-8s [%(short_name)s] %(message)s",
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

    codes: list[str]                # 股票代码列表，如 ["000001.SZ", "600000.SH"]
    start_time: str | None = None   # 开始日期，YYYY-MM-DD 或 ISO datetime（如 2024-01-01T00:00:00）；可选
    end_time: str | None = None     # 结束日期，同上；可选

    @field_validator("codes")
    @classmethod
    def codes_nonempty(cls, v):
        """codes 必须是非空数组，Pydantic 校验失败自动返回 422。"""
        if not v or len(v) == 0:
            raise ValueError("codes must be a non-empty array")
        return v


class MinuteRequest(BaseModel):
    """POST /minute 请求体。period 可选，默认 min1。"""

    codes: list[str]
    period: str | None = None
    start_time: str | None = None
    end_time: str | None = None

    @field_validator("codes")
    @classmethod
    def codes_nonempty(cls, v):
        if not v or len(v) == 0:
            raise ValueError("codes must be a non-empty array")
        return v


class AdjFactorRequest(BaseModel):
    """POST /adj_factor 请求体。字段名与 /daily 一致，由外部项目 YAML field_map 适配。"""

    codes: list[str]                # 股票代码列表，如 ["000001.SZ", "600000.SH"]
    start_time: str | None = None   # 开始日期，YYYY-MM-DD 或 ISO datetime；可选
    end_time: str | None = None     # 结束日期，同上；可选

    @field_validator("codes")
    @classmethod
    def codes_nonempty(cls, v):
        """codes 必须是非空数组，Pydantic 校验失败自动返回 422。"""
        if not v or len(v) == 0:
            raise ValueError("codes must be a non-empty array")
        return v


class EtfNetInflowRequest(BaseModel):
    """POST /etf/net_inflow 请求体。start_time/end_time 可选。"""
    start_time: str | None = None
    end_time: str | None = None


class SdkGate:
    """并发 SDK 调用闸门：在飞调用超过上限时快速失败返回 503，避免线程堆积雪崩。

    gateway._lock 已串行化 SDK 调用，但默认线程池 40 线程会全部排队堆积。
    SdkGate 在路由层限制"在飞"的 SDK 调用数，超出的立即 503，配合 try_acquire/release。
    线程安全：内部 threading.Lock，持锁时间极短（仅计数器增减）。
    """

    def __init__(self, max_concurrent: int = 5):
        self._max = max_concurrent
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._max:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def create_app(config: Config | None = None, gateway: Gateway | None = None) -> FastAPI:
    """创建 FastAPI 应用实例。

    config/gateway 可选注入：测试时传 FakeGateway，生产时默认从环境变量
    创建 Config + AmazingDataGateway。模块级 app = create_app() 供 uvicorn 直接引用。
    """
    if config is None:
        config = Config.from_env()
    if gateway is None:
        gateway = AmazingDataGateway(config)

    kline_service = KlineService(gateway)
    realtime_service = RealtimeService(gateway)
    adj_factor_service = AdjFactorService(gateway)
    etf_flow_service = EtfFlowService(gateway)
    health_service = HealthService(config, gateway, realtime_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时登录 SDK + 后台订阅初始化；退出时停止订阅 + 登出。

        替代已废弃的 on_event("startup"/"shutdown")。uvicorn 触发 lifespan
        startup/shutdown，TestClient 的 with 语法同样触发。
        """
        _align_uvicorn_log_format()
        try:
            config.validate_auth()
        except ValueError as e:
            logger.error("认证配置无效: %s", e)
            raise  # 进程退出，uvicorn 启动失败
        if config.is_configured():
            try:
                gateway.login()
                logger.info("启动登录成功")
                # 订阅调度器：后台线程定期检查窗口，自动启动/停止订阅。
                # 解决 lifespan 只检查一次窗口的问题（非交易时段启动后进入交易时段无自动启动）。
                scheduler = SubscriptionScheduler(gateway, realtime_service, config)
                app.state.subscription_scheduler = scheduler
                scheduler.start()
            except Exception as e:
                logger.error("启动登录失败: %s: %s", type(e).__name__, e)
        else:
            logger.warning("配置不完整，跳过启动登录")
        yield
        # shutdown
        scheduler = getattr(app.state, "subscription_scheduler", None)
        if scheduler:
            scheduler.stop()
            # 等待当前 tick 完成，防止 scheduler 正在 start_subscription 时
            # shutdown 同时 stop_subscription 造成竞态（两者都不走 gateway._lock）
            if scheduler._thread:
                scheduler._thread.join(timeout=5)
        realtime_service.stop_watchdog()
        try:
            gateway.stop_subscription()
        except Exception as e:
            logger.warning("关闭时停止订阅异常: %s: %s", type(e).__name__, e)
        try:
            gateway.logout()
            logger.info("关闭时已登出")
        except Exception as e:
            logger.warning("关闭时登出失败: %s: %s", type(e).__name__, e)

    app = FastAPI(title="AmazingData HTTP Adapter", lifespan=lifespan)
    # Starlette ≥1.x 的 add_middleware 用 insert(0,...)，后 add 的位于最外层 = 最先执行。
    # 故 RequestIdMiddleware 后 add → 最外层 → 先执行 → 设置 request.state.request_id，
    # AuthMiddleware 才能在 401 响应中读到正确 request_id（顺序写反则 401 的 request_id 恒为 "unknown"）。
    app.add_middleware(AuthMiddleware, token=config.auth_token, enabled=config.auth_required)
    app.add_middleware(RequestIdMiddleware)

    app.state.config = config
    app.state.gateway = gateway
    app.state.kline_service = kline_service
    app.state.realtime_service = realtime_service
    app.state.health_service = health_service
    app.state.adj_factor_service = adj_factor_service
    app.state.etf_flow_service = etf_flow_service
    app.state.sdk_gate = SdkGate(max_concurrent=config.sdk_max_concurrent)

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
        logger.info("request_id=%s /daily codes=%d %s..%s",
                    get_request_id(request), len(req.codes),
                    req.start_time or "(default)", req.end_time or "(default)")
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            # kline_service.query 是同步阻塞 SDK 调用，放线程池避免阻塞 event loop
            data = await asyncio.to_thread(
                kline_service.query, req.codes, req.start_time, req.end_time
            )
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
            logger.error("未处理异常: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()

    @app.post("/minute")
    async def minute(req: MinuteRequest, request: Request):
        """分钟K查询。period 可选（默认 min1），白名单 min1~min120。返回 {"data": [...]}。"""
        period = req.period or "min1"
        if period not in MINUTE_PERIODS:
            raise AppError(INVALID_REQUEST, f"unsupported period: {period}", 422)
        logger.info("request_id=%s /minute codes=%d period=%s %s..%s",
                    get_request_id(request), len(req.codes), period,
                    req.start_time or "(default)", req.end_time or "(default)")
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            # kline_service.query 是同步阻塞 SDK 调用，放线程池避免阻塞 event loop
            data = await asyncio.to_thread(
                kline_service.query, req.codes, req.start_time, req.end_time, period=period
            )
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
            logger.error("未处理异常: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()

    @app.post("/adj_factor")
    async def adj_factor(req: AdjFactorRequest, request: Request):
        """除权因子查询。返回 {"data": [{code, trade_date, adj_factor}]}。

        start_time / end_time 可选；未传时返回全量除权事件。
        SDK get_adj_factor 不支持日期参数，由 AdjFactorService 服务端过滤 trade_date。
        """
        logger.info("request_id=%s /adj_factor codes=%d %s..%s",
                    get_request_id(request), len(req.codes),
                    req.start_time or "(default)", req.end_time or "(default)")
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            # adj_factor_service.query 是同步阻塞 SDK 调用，放线程池避免阻塞 event loop
            data = await asyncio.to_thread(
                app.state.adj_factor_service.query, req.codes, req.start_time, req.end_time
            )
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
            logger.error("未处理异常: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()

    @app.post("/etf/net_inflow")
    async def etf_net_inflow(req: EtfNetInflowRequest, request: Request):
        """宽基 ETF 净流入统计。返回 {"data": [...]}。

        start_time / end_time 可选；未传时返回全量数据。
        日期格式校验与 start<=end 校验在 EtfFlowService 内完成，ValueError 转 422。
        """
        logger.info("request_id=%s /etf/net_inflow %s..%s",
                    get_request_id(request),
                    req.start_time or "(default)", req.end_time or "(default)")
        if not app.state.sdk_gate.try_acquire():
            raise AppError(SERVICE_BUSY, "SDK concurrency limit reached, try again later", 503)
        try:
            data = await asyncio.to_thread(
                app.state.etf_flow_service.query, req.start_time, req.end_time
            )
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
            logger.error("未处理异常: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()

    @app.get("/realtime")
    async def realtime(request: Request, codes: str | None = None, types: str | None = None):
        """实时行情快照。优先读订阅缓存（盘中实时），缓存空时 fallback 查当日历史快照。

        codes 为逗号分隔的代码字符串（如 ?codes=000001.SZ,600000.SH），
        从全市场缓存中过滤返回；不传则返回全市场。
        types 为逗号分隔的证券类型（如 ?types=stock,etf），合法值 stock/index/etf，
        非法值返回 422。codes 与 types 叠加过滤：先按 codes，再按 types。
        """
        logger.info("request_id=%s /realtime codes=%s types=%s",
                    get_request_id(request), codes or "(all)", types or "(all)")
        code_list = [s.strip() for s in codes.split(",") if s.strip()] if codes else None
        # 解析 types 参数
        type_set = {t.strip() for t in types.split(",") if t.strip()} if types else None
        if type_set is not None:
            valid_types = {"stock", "index", "etf"}
            invalid = type_set - valid_types
            if invalid:
                raise AppError(
                    INVALID_REQUEST,
                    f"invalid types: {','.join(sorted(invalid))}, allowed: stock,index,etf",
                    422,
                )
        # 优先读订阅缓存（盘中实时推送的数据）；snapshot 只读内存，不阻塞 event loop
        data = realtime_service.snapshot(code_list, type_set)
        if not data:
            # 缓存空（非交易时段/订阅未推送），fallback 查当日历史快照。
            # query_snapshot 是同步阻塞 SDK 调用（全市场可能数分钟），必须放线程池，
            # 否则卡死 event loop 导致 /health 等其他请求全部阻塞。
            try:
                data = await asyncio.to_thread(
                    realtime_service.fallback_snapshot, code_list, type_set,
                )
            except GatewayNotReadyError as e:
                logger.info("request_id=%s realtime fallback: SDK 未就绪",
                            get_request_id(request))
                raise AppError(SDK_NOT_READY, str(e), 503)
            except Exception as e:
                logger.warning("request_id=%s realtime fallback 失败: %s: %s",
                               get_request_id(request), type(e).__name__, e)
                data = []
            else:
                logger.info("request_id=%s realtime fallback: %d 代码 -> %d 条",
                            get_request_id(request),
                            len(code_list) if code_list else 0, len(data))
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
        logger.warning("request_id=%s 请求校验失败: errors=%s body=%s",
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

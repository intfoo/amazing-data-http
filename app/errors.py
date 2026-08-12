"""统一错误码、AppError 异常和 request_id 中间件。

错误响应格式：{"error": {"code": "...", "message": "...", "request_id": "..."}}
request_id 贯穿日志和响应，便于从 HTTP 响应反查服务端日志。
"""

import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# 错误码 → HTTP 状态码映射（在 http_app.py 的异常处理中使用）
INVALID_REQUEST = "INVALID_REQUEST"          # 422：请求体、代码列表或日期参数无效
SDK_NOT_READY = "SDK_NOT_READY"              # 503：SDK 未初始化或未登录
SDK_QUERY_FAILED = "SDK_QUERY_FAILED"        # 502：上游 SDK 查询失败
SERIALIZATION_FAILED = "SERIALIZATION_FAILED"  # 502：返回值无法安全序列化
REALTIME_SUBSCRIPTION_FAILED = "REALTIME_SUBSCRIPTION_FAILED"  # 503：实时订阅未启动或已崩溃
INTERNAL_ERROR = "INTERNAL_ERROR"            # 500：未分类的内部错误
SERVICE_BUSY = "SERVICE_BUSY"                # 503：并发 SDK 调用超限，快速失败
UNAUTHORIZED = "UNAUTHORIZED"                # 401：缺失或无效的 Bearer token


class AppError(Exception):
    """携带错误码和 HTTP 状态码的业务异常，由全局 handler 统一格式化。"""

    def __init__(self, code: str, message: str, status_code: int = 500):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求分配 request_id（优先使用客户端 X-Request-ID 头，否则生成 UUID）。

    request_id 写入 request.state 供路由读取，同时回写到响应头，
    使客户端能凭此 ID 在服务端日志中定位完整请求上下文。
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def get_request_id(request: Request) -> str:
    """从 request.state 获取 request_id，兜底返回 "unknown"。"""
    return getattr(request.state, "request_id", "unknown")

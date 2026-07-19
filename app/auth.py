"""Bearer Token 认证中间件。

AUTH_REQUIRED=true 时，除白名单路径（/health）外所有请求必须带
Authorization: Bearer <token> 头且 token 与 AUTH_TOKEN 一致。
token 比对用 hmac.compare_digest（常数时间，防时序攻击）。
scheme 名大小写不敏感（RFC 6750 §2.1）。
"""

import hmac

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.errors import UNAUTHORIZED, get_request_id

# 白名单路径：不经过认证。用 path 去掉首尾斜杠后比较，兼容 /health、/health/ 等。
PUBLIC_PATHS = {"health"}


class AuthMiddleware(BaseHTTPMiddleware):
    """Bearer token 认证中间件。

    - enabled=False 时直接放行所有请求（auth_required=false 模式）
    - /health 路径直接放行（Docker healthcheck 约束）
    - 其余路径要求 Authorization: Bearer <token>，token 比对用常数时间算法
    - 401 响应遵循现有错误信封格式 + WWW-Authenticate: Bearer 头（RFC 6750）
    - scheme 名大小写不敏感：bearer/Bearer/BEARER 均接受
    """

    def __init__(self, app, token: str, enabled: bool):
        super().__init__(app)
        self._token = token
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)
        # path 去掉首尾斜杠后比较，兼容 /health 与 /health/
        if request.url.path.strip("/") in PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        # RFC 6750 §2.1: scheme 名大小写不敏感。用 partition 拆分，避免硬编码 len("Bearer ")
        scheme, sep, presented = auth_header.partition(" ")
        if not sep or scheme.lower() != "bearer":
            return self._unauthorized(request, "missing or malformed Authorization header")
        presented = presented.strip()
        if not presented:
            return self._unauthorized(request, "empty bearer token")

        if not hmac.compare_digest(presented.encode(), self._token.encode()):
            return self._unauthorized(request, "invalid bearer token")

        return await call_next(request)

    @staticmethod
    def _unauthorized(request: Request, message: str) -> JSONResponse:
        request_id = get_request_id(request)
        return JSONResponse(
            status_code=401,
            content={"error": {
                "code": UNAUTHORIZED,
                "message": message,
                "request_id": request_id,
            }},
            headers={"WWW-Authenticate": "Bearer"},
        )

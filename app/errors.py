import uuid
from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

INVALID_REQUEST = "INVALID_REQUEST"
SDK_NOT_READY = "SDK_NOT_READY"
SDK_QUERY_FAILED = "SDK_QUERY_FAILED"
SERIALIZATION_FAILED = "SERIALIZATION_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 500):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")

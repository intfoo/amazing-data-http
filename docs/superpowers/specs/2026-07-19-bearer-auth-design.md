# Bearer Token 认证设计

**日期**: 2026-07-19
**状态**: Approved
**作者**: brainstorming session

## 1. 背景与目标

`amazingdata-http` 是 FastAPI 实现的 AmazingData SDK HTTP 适配器，当前所有接口裸奔无认证。需新增 Bearer Token 认证：

- token 从 docker-compose 环境变量取（`.env` 文件注入）
- 认证通过才能调用数据接口
- `/health` 保持免认证（Docker healthcheck 约束）

### 暴露端点现状

| 端点 | 方法 | 用途 | 认证 |
|------|------|------|------|
| `/health` | GET | Docker healthcheck + 诊断 | **免认证** |
| `/daily` | POST | 日 K 查询 | 需认证 |
| `/minute` | POST | 分钟 K 查询 | 需认证 |
| `/adj_factor` | POST | 除权因子查询 | 需认证 |
| `/realtime` | GET | 实时行情快照 | 需认证 |

## 2. 设计决策

### 2.1 `/health` 豁免认证

Docker Compose healthcheck 命令为 `urllib.request.urlopen('http://localhost:3021/health')`，无法携带 Authorization 头。`/health` 必须免认证，否则容器被标记为 unhealthy。

### 2.2 双环境变量配置

| 环境变量 | 默认 | 含义 |
|---------|------|------|
| `AUTH_TOKEN` | `""` | Bearer token，客户端访问数据接口需在 `Authorization: Bearer <token>` 头中携带 |
| `AUTH_REQUIRED` | `true` | 认证开关。`false` 时认证**完全关闭**，所有请求直接放行，`AUTH_TOKEN` 字段被忽略 |

### 2.3 Token 强度校验规则（启动时）

仅在 `AUTH_REQUIRED=true` 时校验，失败抛 `ValueError` 阻止进程启动：

1. token 非空
2. `len(token) > 12`
3. 同时含字母（`c.isalpha()`）和数字（`c.isdigit()`）

### 2.4 开关语义：完全关闭模式

`AUTH_REQUIRED=false` → 认证彻底关闭，所有请求直接放行（不管带不带 Authorization 头）。token 字段不读、不校验、不比对。

**为何不用"软模式"（带头才校验）**：软模式让攻击者只要不带 Authorization 头就能绕过认证，等于没认证，且带来"配了 token 就安全"的错觉。完全关闭模式更直白：要么全开要么全关，没有中间态误用风险。

### 2.5 实现方案：Middleware

新增 `AuthMiddleware`，与现有 `RequestIdMiddleware` 并列。

**为何选 Middleware 而非 FastAPI Dependency**：
- 默认安全：新加路由自动受保护，避免"忘加 `Depends`" 导致的安全漏洞
- 与现有代码风格一致：项目已在用 `RequestIdMiddleware` 模式
- 改动面最小：只在 `create_app` 加一行 `add_middleware` + 新建 `app/auth.py`，不动任何路由函数

**中间件注册顺序**（关键）：

```python
app.add_middleware(AuthMiddleware, token=config.auth_token, enabled=config.auth_required)
app.add_middleware(RequestIdMiddleware)
```

Starlette 中间件按"后添加先执行"。`RequestIdMiddleware` 后 add → 最先执行 → `request.state.request_id` 已设置 → `AuthMiddleware` 能读到正确 `request_id` 用于 401 响应。

### 2.6 Token 比对：常数时间比较

使用 `hmac.compare_digest(presented.encode(), expected.encode())` 防时序攻击。长度不同时 `compare_digest` 仍安全返回 `False`。

## 3. 组件设计

### 3.1 `app/config.py` 扩展

```python
@dataclass(frozen=True)
class Config:
    # ... 现有字段 ...
    auth_token: str = ""
    auth_required: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            # ... 现有字段 ...
            auth_token=os.environ.get("AUTH_TOKEN", ""),
            auth_required=os.environ.get("AUTH_REQUIRED", "true").lower()
                          in ("1", "true", "yes", "on"),
        )

    def validate_auth(self) -> None:
        """启动时校验 auth 配置。失败抛 ValueError。
        仅当 auth_required=True 时校验，false 时 token 字段被忽略。
        """
        if not self.auth_required:
            return
        if not self.auth_token:
            raise ValueError("AUTH_TOKEN is required when AUTH_REQUIRED=true")
        if len(self.auth_token) <= 12:
            raise ValueError(
                f"AUTH_TOKEN too short: {len(self.auth_token)} chars, need > 12"
            )
        has_alpha = any(c.isalpha() for c in self.auth_token)
        has_digit = any(c.isdigit() for c in self.auth_token)
        if not (has_alpha and has_digit):
            raise ValueError("AUTH_TOKEN must contain both letters and digits")
```

### 3.2 `app/errors.py` 新增错误码

```python
UNAUTHORIZED = "UNAUTHORIZED"  # 401：缺失或无效的 Bearer token
```

### 3.3 `app/auth.py` 新建（核心组件）

```python
"""Bearer Token 认证中间件。

AUTH_REQUIRED=true 时，除白名单路径（/health）外所有请求必须带
Authorization: Bearer <token> 头且 token 与 AUTH_TOKEN 一致。
token 比对用 hmac.compare_digest（常数时间，防时序攻击）。
"""

import hmac

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.errors import UNAUTHORIZED, get_request_id

# 白名单路径：不经过认证
PUBLIC_PATHS = {"/health"}


class AuthMiddleware(BaseHTTPMiddleware):
    """Bearer token 认证中间件。

    - enabled=False 时直接放行所有请求（auth_required=false 模式）
    - /health 路径直接放行（Docker healthcheck 约束）
    - 其余路径要求 Authorization: Bearer <token>，token 比对用常数时间算法
    - 401 响应遵循现有错误信封格式 + WWW-Authenticate: Bearer 头（RFC 6750）
    """

    def __init__(self, app, token: str, enabled: bool):
        super().__init__(app)
        self._token = token
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return self._unauthorized(request, "missing or malformed Authorization header")
        presented = auth_header[len("Bearer "):].strip()
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
```

### 3.4 `app/http_app.py` 改动

在 `lifespan` 最开头（任何 SDK login 之前）调用 `config.validate_auth()`：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    _align_uvicorn_log_format()
    try:
        config.validate_auth()
    except ValueError as e:
        logger.error("auth config invalid: %s", e)
        raise  # 进程退出，uvicorn 启动失败
    # ... 后续 SDK login 逻辑 ...
```

> 校验放 lifespan 而非 `create_app()` 顶层：让测试中 `create_app()` 可构造任意 Config 而不被启动校验阻塞；校验只在真正启动（uvicorn / `TestClient` with 块）时触发。

注册中间件（注意顺序）：

```python
app = FastAPI(title="AmazingData HTTP Adapter", lifespan=lifespan)
app.add_middleware(AuthMiddleware, token=config.auth_token, enabled=config.auth_required)
app.add_middleware(RequestIdMiddleware)
```

### 3.5 `app/health.py` 扩展

`status()` 增加 `auth` 字段，仅暴露状态枚举，不暴露 token 本身：

```python
def status(self) -> dict:
    ready = self._config.is_configured() and self._gw.is_ready()
    rt = self._realtime_svc.is_active() if self._realtime_svc else False
    if not self._config.auth_required:
        auth_state = "disabled"
    elif self._config.auth_token:
        auth_state = "configured"
    else:
        auth_state = "misconfigured"
    return {
        "status": "ok" if ready else "degraded",
        "sdk": "ready" if self._gw.is_ready() else "not_ready",
        "config": "complete" if self._config.is_configured() else "incomplete",
        "realtime": "active" if rt else "inactive",
        "auth": auth_state,
    }
```

> 启动校验已拦截 `misconfigured` 情况（进程起不来），此处 `misconfigured` 仅作防御性输出。

## 4. 错误响应

### 401 响应格式

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
X-Request-ID: <uuid>
Content-Type: application/json

{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "missing or malformed Authorization header",
    "request_id": "<uuid>"
  }
}
```

`message` 三种值：
- `"missing or malformed Authorization header"` — 缺失或非 `Bearer ` 前缀
- `"empty bearer token"` — `Bearer ` 后为空
- `"invalid bearer token"` — token 比对失败

## 5. 配置文件更新

### 5.1 `.env.example`

新增 Bearer Token 认证章节：

```bash
# ===== Bearer Token 认证 =====
# 客户端访问 /daily /minute /adj_factor /realtime 必须带
# Authorization: Bearer <AUTH_TOKEN> 头。/health 不需要认证。
# AUTH_TOKEN 强度要求：长度 > 12 且同时含字母和数字。
AUTH_TOKEN=

# 是否启用认证（默认 true）。
# false 时认证彻底关闭，所有请求直接放行，AUTH_TOKEN 字段被忽略。
# 仅本地调试用，生产环境必须保持 true。
AUTH_REQUIRED=true
```

### 5.2 `docs/API.md`

新增"认证"章节，覆盖：
- 4 个数据接口需 `Authorization: Bearer <token>` 头
- `/health` 免认证
- 401 响应格式与示例
- 配置项说明

## 6. 测试策略

### 6.1 现有测试零改动策略

`tests/test_http_app.py` 的 `make_test_app()` 默认 `auth_required=False`，现有 30+ 测试无需修改。

```python
def make_test_app(gateway=None, auth_token="", auth_required=False):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
    )
    ...
```

### 6.2 新建 `tests/test_auth.py`

| 用例 | 验证点 |
|------|--------|
| `test_auth_disabled_allows_all` | `AUTH_REQUIRED=false` 时无 header 也 200 |
| `test_auth_enabled_no_header_returns_401` | 缺 Authorization → 401 + `UNAUTHORIZED` + `WWW-Authenticate` 头 |
| `test_auth_enabled_malformed_header_returns_401` | 非 `Bearer ` 前缀 → 401 |
| `test_auth_enabled_wrong_token_returns_401` | 错误 token → 401 |
| `test_auth_enabled_correct_token_returns_200` | 正确 token → 200 |
| `test_health_no_auth_required` | `/health` 无 header → 200/503（不 401） |
| `test_auth_401_has_request_id` | 401 响应带 `request_id` + `X-Request-ID` 头 |
| `test_auth_401_envelope_format` | 401 响应体 `{"error": {"code", "message", "request_id"}}` |
| `test_config_validate_auth_too_short` | `validate_auth()` 对短 token 抛 ValueError |
| `test_config_validate_auth_no_alpha_or_digit` | 纯字母/纯数字 token 抛 ValueError |
| `test_config_validate_auth_disabled_skips` | `auth_required=false` 时弱 token 不抛 |
| `test_config_validate_auth_empty_when_required` | required=true 但 token 空 → ValueError |
| `test_health_status_includes_auth_field` | `/health` 响应含 `auth` 字段 |
| `test_lifespan_fails_on_invalid_auth_config` | 启动时弱 token → 进程退出（TestClient with 块抛异常） |

### 6.3 测试 helper

```python
VALID_TOKEN = "test-token-abc123"  # len=18, 含字母+数字

def make_auth_app(auth_required=True, auth_token=VALID_TOKEN, gateway=None):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
    )
    if gateway is None:
        gateway = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    return TestClient(create_app(config=config, gateway=gateway))
```

## 7. 改动清单

| 文件 | 改动类型 | 改动内容 |
|------|---------|---------|
| `app/config.py` | 修改 | +2 字段 +`validate_auth()` 方法 +`from_env` 读 2 个新 env |
| `app/errors.py` | 修改 | +`UNAUTHORIZED` 错误码常量 |
| `app/auth.py` | **新建** | `AuthMiddleware` 实现 |
| `app/http_app.py` | 修改 | lifespan 调 `validate_auth()`；`add_middleware(AuthMiddleware, ...)` |
| `app/health.py` | 修改 | `status()` 加 `auth` 字段 |
| `.env.example` | 修改 | +`AUTH_TOKEN` +`AUTH_REQUIRED` 章节 |
| `docs/API.md` | 修改 | +认证章节 |
| `tests/test_auth.py` | **新建** | 14 个测试用例 |
| `tests/test_http_app.py` | 修改 | `make_test_app` 加 `auth_required=False` 默认 + `health` 测试断言 `auth` 字段 |

## 8. 安全考量

1. **常数时间比较**：`hmac.compare_digest` 防时序攻击
2. **token 不入日志**：401 响应 message 只描述错误类型，不记录 presented token
3. **token 不入 health 响应**：`/health` 只暴露 `auth: configured/misconfigured/disabled` 状态枚举
4. **启动校验**：弱 token 直接阻止启动，避免生产环境误配
5. **默认安全**：`AUTH_REQUIRED` 默认 `true`，本地调试需显式关闭
6. **白名单最小化**：仅 `/health` 豁免，其他路径（包括未知的）都需认证

## 9. 非目标 (Out of Scope)

- 多 token 支持（当前单 token，足够内部服务用）
- token 轮换/热更新（需重启服务生效）
- JWT 校验（用静态 token，无需签发/过期机制）
- 速率限制（与认证正交，未来单独加）
- IP 白名单（与认证正交）

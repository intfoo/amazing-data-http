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

**中间件注册顺序**（关键，已核实 Starlette 源码 `.venv/Lib/site-packages/starlette/applications.py:98-101`）：

```python
app.add_middleware(AuthMiddleware, token=config.auth_token, enabled=config.auth_required)
app.add_middleware(RequestIdMiddleware)   # 后 add = 外层 = 最先执行
```

**执行顺序原理**（已通过实测 + 源码核实）：

`Starlette.add_middleware` 用 `self.user_middleware.insert(0, Middleware(...))` — **插入到列表头部**。`build_middleware_stack` 用 `reversed(middleware)` 包装。

追踪：先 `add(A)` 再 `add(B)` → `user_middleware = [B, A]` → `reversed = [..., A, B, ...]` → 包装顺序 `A` 先包（内层）、`B` 后包（外层）→ 最终栈 `ServerErr → B → A → Exception → router` → **B（后 add 的）在最外层 = 最先执行**。

因此 `RequestIdMiddleware` **后** add → 最先执行 → `request.state.request_id` 已设置 → `AuthMiddleware` 能读到正确 `request_id` 用于 401 响应。**顺序写反（先 add RequestIdMiddleware）会导致 401 响应的 `request_id` 恒为 `"unknown"`，`X-Request-ID` 头缺失**（已由 `test_auth_401_has_request_id` 实测验证）。

**`BaseHTTPMiddleware` 已知限制评估**：该基类对流式响应（SSE/大文件）有缓冲副作用，异常链路较复杂。本项目所有路由均返回 `JSONResponse`（无流式场景），且现有 `RequestIdMiddleware` 已采用同基类无问题，故适用。

### 2.6 Token 比对：常数时间比较

使用 `hmac.compare_digest(presented.encode(), expected.encode())` 防时序攻击。

**已知限制**（Python 官方文档）：当两参数长度不同时，返回值仍正确为 `False`，但**不保证常数时间**，理论上会泄漏 token 长度信息。token 长度非高敏感信息（且攻击者需大量时序探测），接受此折衷。

## 3. 组件设计

### 3.1 `app/config.py` 扩展

**字段默认值设计**（关键决策）：

- **环境变量 `AUTH_REQUIRED` 默认 `"true"`**：保证生产环境（通过 `Config.from_env()` 构造）默认安全
- **dataclass 字段 `auth_required` 默认 `False`**：让测试中直接 `Config(username="u", ...)` 构造的实例默认关闭认证，避免破坏现有 30+ 测试

两套默认值不冲突——`from_env()` 显式读 env 覆盖字段默认值；只有"直接构造 Config 不传 auth_required"的场景（基本只在测试）才会拿到 `False`。

```python
@dataclass(frozen=True)
class Config:
    # ... 现有字段 ...
    auth_token: str = ""
    auth_required: bool = False  # 字段默认 False（测试便利）；env 默认 "true"（生产安全）

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            # ... 现有字段 ...
            auth_token=os.environ.get("AUTH_TOKEN", ""),
            auth_required=os.environ.get("AUTH_REQUIRED", "true").lower()
                          in ("1", "true", "yes", "on"),
        )

    def is_auth_valid(self) -> bool:
        """返回 auth 配置是否有效。与 is_configured() 同风格（查询方法）。

        - auth_required=False：始终返回 True（认证关闭，token 被忽略）
        - auth_required=True：token 非空 + 长度>12 + 含字母和数字
        """
        if not self.auth_required:
            return True
        if not self.auth_token:
            return False
        if len(self.auth_token) <= 12:
            return False
        has_alpha = any(c.isalpha() for c in self.auth_token)
        has_digit = any(c.isdigit() for c in self.auth_token)
        return bool(has_alpha and has_digit)

    def validate_auth(self) -> None:
        """启动时校验 auth 配置。失败抛 ValueError，由 lifespan 捕获后进程退出。
        内部委托给 is_auth_valid()，保证与 HealthService.status() 同口径。
        """
        if self.is_auth_valid():
            return
        if not self.auth_token:
            raise ValueError("AUTH_TOKEN is required when AUTH_REQUIRED=true")
        if len(self.auth_token) <= 12:
            raise ValueError(
                f"AUTH_TOKEN too short: {len(self.auth_token)} chars, need > 12"
            )
        raise ValueError("AUTH_TOKEN must contain both letters and digits")
```

> **风格一致性**：现有 `Config.is_configured()` 返回 `bool`，新增 `is_auth_valid()` 同风格。`validate_auth()` 是 `is_auth_valid()` 的"抛异常版"包装，供 lifespan fail-fast 用；`HealthService.status()` 直接复用 `is_auth_valid()` 判断 `configured/misconfigured`，避免两套口径。

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
```

**已知未来扩展点**（当前不实现，文档留痕）：
- **CORS preflight**：若未来加 CORS 中间件，OPTIONS 请求会被本中间件 401 拦截。届时需在 `dispatch` 开头加 `if request.method == "OPTIONS": return await call_next(request)`，或协调 CORSMiddleware 与 AuthMiddleware 的顺序（CORS 应在外层先处理 preflight）。
- **Trailing slash**：当前用 `path.strip("/")` 兼容 `/health` 与 `/health/`，但不会自动重定向。FastAPI 默认 `redirect_slashes=True` 仍生效，但重定向发生在路由层而非中间件层。

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

> **校验放 lifespan 而非 `create_app()` 顶层的设计取舍**：
>
> 1. **测试便利**：`create_app()` 可构造任意 Config 而不被启动校验阻塞，测试中 `TestClient(app)` 不进 `with` 块就不触发校验
> 2. **已知折衷**：模块级 `app = create_app()`（`http_app.py:413`）在 import 时执行，`Config.from_env()` 读 env。若生产环境配了 `AUTH_REQUIRED=true` 但 `AUTH_TOKEN` 为空/弱，`create_app()` 不报错（校验在 lifespan），进程能被 uvicorn 拉起、开始监听端口，**直到 lifespan startup 才抛异常退出**。这段窗口期（端口已开但进程即将死）虽短（通常亚秒级），但容器编排器可能误判端口已就绪。
> 3. **缓解措施**：`docker-compose.yml` 的 `healthcheck.start_period: 60s` 覆盖该窗口期；healthcheck 在 startup 失败后 `/health` 不可达，容器最终被标记 unhealthy 并重启。

注册中间件（注意顺序，详见 2.5）：

```python
app = FastAPI(title="AmazingData HTTP Adapter", lifespan=lifespan)
app.add_middleware(AuthMiddleware, token=config.auth_token, enabled=config.auth_required)
app.add_middleware(RequestIdMiddleware)  # 后 add = 外层 = 最先执行
```

### 3.5 `app/health.py` 扩展

`status()` 增加 `auth` 字段，仅暴露状态枚举，不暴露 token 本身。**复用 `Config.is_auth_valid()`** 保证与启动校验同口径（避免弱 token 被报为 `configured`）：

```python
def status(self) -> dict:
    ready = self._config.is_configured() and self._gw.is_ready()
    rt = self._realtime_svc.is_active() if self._realtime_svc else False
    # 复用 is_auth_valid() 保证与 lifespan validate_auth() 同口径
    if not self._config.auth_required:
        auth_state = "disabled"
    elif self._config.is_auth_valid():
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
> 字段风格说明：现有字段都是二态字符串（`ok/degraded`、`ready/not_ready`），`auth` 引入三态是因为"认证关闭"是合法且常见的运行状态（本地调试），需与"配置错误"区分。

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
#
# 在本地模式与 Docker 模式下均生效（docker-compose.yml 通过 env_file: .env 注入容器），
# 与 HTTP_HOST 不同（后者在 Docker 模式下被 Dockerfile CMD 覆盖）。
AUTH_TOKEN=

# 是否启用认证（默认 true）。
# false 时认证彻底关闭，所有请求直接放行，AUTH_TOKEN 字段被忽略。
# 仅本地调试用，生产环境必须保持 true。
AUTH_REQUIRED=true
```

### 5.2 `docs/API.md`

现有结构（无编号，纯 `##` 标题）：`POST /daily` → `POST /minute` → `POST /adj_factor` → `GET /realtime` → `GET /health` → `错误响应格式`。

**改动**：
1. **新增"认证"章节**：置于 `GET /health` 之后、`错误响应格式` 之前。内容包括：
   - 4 个数据接口需 `Authorization: Bearer <token>` 头
   - `/health` 免认证
   - 401 响应格式与示例
   - 配置项说明（`AUTH_TOKEN` / `AUTH_REQUIRED` 环境变量）
2. **在 `错误响应格式` 的错误码表中追加一行**：`UNAUTHORIZED | 401 | 缺失或无效的 Bearer token`

## 6. 测试策略

### 6.1 现有测试零改动策略（已核实）

**关键设计**：`Config.auth_required` 字段默认 `False`（见 3.1），让所有直接 `Config(username="u", ...)` 构造的测试实例自动关闭认证。环境变量 `AUTH_REQUIRED` 默认 `"true"` 仅通过 `Config.from_env()` 生效，与字段默认值不冲突。

**已核实的现有测试构造点**（全部默认 `auth_required=False`，无需改动）：
- `tests/test_http_app.py`：
  - `make_test_app()` 内 1 处（第 10 行）
  - 直接 `Config(...)` 构造 9 处（第 309、343、383、407、429、451、464、481 行等）
- `tests/test_adj_factor.py`：
  - `make_test_app()` 内 1 处（第 130 行）

**现有 `/health` 测试不受影响**（已逐条核实）：
- `test_health_ok`：只断言 `body["status"]` 和 `body["sdk"]`，加 `auth` 字段不破坏
- `test_health_degraded_when_not_ready`：只断言 `body["sdk"]`
- `test_health_no_secrets_in_response`：检查 `"password" not in body_text.lower()`，`auth: configured` 不含 password
- `test_health_has_realtime_field`：只检查 `"realtime" in resp.json()`

**`make_test_app` 修改**（仅 `tests/test_http_app.py` 和 `tests/test_adj_factor.py` 两处，加可选参数）：

```python
def make_test_app(gateway=None, auth_token="", auth_required=False):
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
    )
    ...
```

默认 `auth_required=False` 保持现有调用零改动；新认证测试传 `auth_required=True`。

### 6.2 新建 `tests/test_auth.py`

| 用例 | 验证点 |
|------|--------|
| `test_auth_disabled_allows_all` | `AUTH_REQUIRED=false` 时无 header 也 200 |
| `test_auth_enabled_no_header_returns_401` | 缺 Authorization → 401 + `UNAUTHORIZED` + `WWW-Authenticate` 头 |
| `test_auth_enabled_malformed_header_returns_401` | 非 `Bearer ` 前缀（如 `Basic xxx`）→ 401 |
| `test_auth_enabled_lowercase_bearer_scheme_accepted` | `bearer xxx`（小写 scheme，RFC 6750 兼容）→ 200 |
| `test_auth_enabled_uppercase_bearer_scheme_accepted` | `BEARER xxx`（大写 scheme）→ 200 |
| `test_auth_enabled_wrong_token_returns_401` | 错误 token → 401 |
| `test_auth_enabled_correct_token_returns_200` | 正确 token → 200 |
| `test_health_no_auth_required` | `/health` 无 header → 200/503（不 401） |
| `test_health_with_trailing_slash_no_auth_required` | `/health/` 无 header → 不 401（路径白名单兼容 trailing slash） |
| `test_auth_401_has_request_id` | 401 响应带 `request_id` + `X-Request-ID` 头（验证中间件顺序正确） |
| `test_auth_401_envelope_format` | 401 响应体 `{"error": {"code", "message", "request_id"}}` |
| `test_config_is_auth_valid_too_short` | `is_auth_valid()` 对短 token 返回 False |
| `test_config_is_auth_valid_no_alpha_or_digit` | 纯字母/纯数字 token 返回 False |
| `test_config_is_auth_valid_disabled_skips` | `auth_required=False` 时弱 token 也返回 True |
| `test_config_is_auth_valid_empty_when_required` | required=true 但 token 空 → False |
| `test_config_validate_auth_raises_on_invalid` | `validate_auth()` 对上述无效配置抛 ValueError |
| `test_health_status_includes_auth_field` | `/health` 响应含 `auth` 字段，值为 `configured`/`disabled`/`misconfigured` 之一 |
| `test_lifespan_fails_on_invalid_auth_config` | 启动时弱 token → `with TestClient(app)` 抛 ValueError |

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
| `app/config.py` | 修改 | +`auth_token` 字段（默认 `""`）+ `auth_required` 字段（默认 `False`，测试便利）+ `from_env` 读 2 个新 env（env 默认 `"true"` 保生产安全）+ `is_auth_valid()` + `validate_auth()` 方法 |
| `app/errors.py` | 修改 | +`UNAUTHORIZED` 错误码常量 |
| `app/auth.py` | **新建** | `AuthMiddleware` 实现（RFC 6750 大小写不敏感 scheme、trailing slash 兼容、常数时间比对） |
| `app/http_app.py` | 修改 | lifespan 调 `validate_auth()`；**先** `add_middleware(AuthMiddleware, ...)` **再** `add_middleware(RequestIdMiddleware)`（顺序关键：后 add = 外层 = 先执行，确保 401 响应有 request_id） |
| `app/health.py` | 修改 | `status()` 加 `auth` 字段，复用 `is_auth_valid()` 保证与启动校验同口径 |
| `.env.example` | 修改 | +`AUTH_TOKEN` +`AUTH_REQUIRED` 章节 + Docker 模式生效说明 |
| `docs/API.md` | 修改 | +认证章节（置于 `GET /health` 后、`错误响应格式` 前）+ 错误码表追加 `UNAUTHORIZED` 行 |
| `tests/test_auth.py` | **新建** | 18 个测试用例（覆盖中间件、Config 校验、health 字段、lifespan 启动失败） |
| `tests/test_http_app.py` | 修改 | `make_test_app` 加 `auth_token=""` + `auth_required=False` 默认参数（保持现有 30+ 测试零改动） |
| `tests/test_adj_factor.py` | 修改 | `make_test_app` 加 `auth_token=""` + `auth_required=False` 默认参数（保持现有 8 个 HTTP 测试零改动） |

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
- **CORS 跨域支持**：当前项目无 CORS 中间件。若未来加 CORS，需协调 CORSMiddleware 与 AuthMiddleware 的顺序，并在 `AuthMiddleware.dispatch` 开头豁免 OPTIONS preflight 请求（见 3.3 已知扩展点）

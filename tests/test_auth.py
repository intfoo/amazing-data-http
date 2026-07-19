"""Bearer Token 认证测试。

覆盖 spec 2026-07-19-bearer-auth-design.md 第 6.2 节列出的 18 个用例：
- AuthMiddleware：401/200 分支、大小写不敏感 scheme、trailing slash 白名单、
  request_id 透传、错误信封格式
- Config：is_auth_valid() / validate_auth() 校验逻辑
- HealthService：auth 字段状态枚举
- lifespan：启动时弱 token 阻止启动
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from tests.conftest import FakeGateway, make_daily_df


VALID_TOKEN = "test-token-abc123"  # len=18, 含字母+数字
WRONG_TOKEN = "wrong-token-xyz789"
SHORT_TOKEN = "short123"           # len=8, 含字母+数字但太短
ALPHA_ONLY = "onlyletters"         # len=11, 无数字
DIGIT_ONLY = "1234567890123"       # len=13, 无字母


def make_auth_app(auth_required=True, auth_token=VALID_TOKEN, gateway=None):
    """构造带认证配置的 TestClient。默认 auth_required=True（与 make_test_app 相反）。"""
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=auth_token, auth_required=auth_required,
    )
    if gateway is None:
        gateway = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    return TestClient(create_app(config=config, gateway=gateway))


# ========== AuthMiddleware 行为测试 ==========

def test_auth_disabled_allows_all():
    """AUTH_REQUIRED=false 时无 Authorization 头也放行。"""
    client = make_auth_app(auth_required=False)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200


def test_auth_enabled_no_header_returns_401():
    """认证开启 + 缺 Authorization 头 → 401 + UNAUTHORIZED + WWW-Authenticate。"""
    client = make_auth_app(auth_required=True)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_auth_enabled_malformed_header_returns_401():
    """非 Bearer 前缀（如 Basic）→ 401。"""
    client = make_auth_app(auth_required=True)
    resp = client.post(
        "/daily",
        json={"codes": ["000001.SZ"]},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401


def test_auth_enabled_lowercase_bearer_scheme_accepted():
    """小写 scheme 'bearer' 按 RFC 6750 应被接受。"""
    client = make_auth_app(auth_required=True)
    resp = client.post(
        "/daily",
        json={"codes": ["000001.SZ"]},
        headers={"Authorization": f"bearer {VALID_TOKEN}"},
    )
    assert resp.status_code == 200


def test_auth_enabled_uppercase_bearer_scheme_accepted():
    """大写 scheme 'BEARER' 按 RFC 6750 应被接受。"""
    client = make_auth_app(auth_required=True)
    resp = client.post(
        "/daily",
        json={"codes": ["000001.SZ"]},
        headers={"Authorization": f"BEARER {VALID_TOKEN}"},
    )
    assert resp.status_code == 200


def test_auth_enabled_wrong_token_returns_401():
    """错误 token → 401。"""
    client = make_auth_app(auth_required=True)
    resp = client.post(
        "/daily",
        json={"codes": ["000001.SZ"]},
        headers={"Authorization": f"Bearer {WRONG_TOKEN}"},
    )
    assert resp.status_code == 401


def test_auth_enabled_correct_token_returns_200():
    """正确 token → 200。"""
    client = make_auth_app(auth_required=True)
    resp = client.post(
        "/daily",
        json={"codes": ["000001.SZ"]},
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert resp.status_code == 200


# ========== /health 白名单测试 ==========

def test_health_no_auth_required():
    """/health 无 Authorization 头不返回 401（Docker healthcheck 约束）。"""
    client = make_auth_app(auth_required=True)
    resp = client.get("/health")
    # 200 或 503 取决于 SDK 就绪状态，但绝不能是 401
    assert resp.status_code != 401


def test_health_with_trailing_slash_no_auth_required():
    """/health/（trailing slash）同样免认证，验证 path.strip("/") 白名单兼容。"""
    client = make_auth_app(auth_required=True)
    resp = client.get("/health/")
    assert resp.status_code != 401


# ========== 401 响应格式与中间件顺序测试 ==========

def test_auth_401_has_request_id():
    """401 响应带 request_id 字段 + X-Request-ID 头。

    验证中间件顺序：RequestIdMiddleware 先执行（外层），AuthMiddleware 后执行，
    故 401 响应能读到已设置的 request_id 并回写响应头。顺序写反则 request_id 恒为 unknown。
    """
    client = make_auth_app(auth_required=True)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 401
    body = resp.json()
    rid = body["error"]["request_id"]
    assert rid and rid != "unknown"
    assert resp.headers.get("X-Request-ID") == rid


def test_auth_401_envelope_format():
    """401 响应体遵循统一错误信封 {"error": {"code", "message", "request_id"}}。"""
    client = make_auth_app(auth_required=True)
    resp = client.post("/daily", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 401
    body = resp.json()
    assert set(body.keys()) == {"error"}
    err = body["error"]
    assert set(err.keys()) == {"code", "message", "request_id"}
    assert err["code"] == "UNAUTHORIZED"
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["request_id"], str) and err["request_id"]


# ========== Config.is_auth_valid() / validate_auth() 测试 ==========

def test_config_is_auth_valid_too_short():
    """短 token（len<=12）且 required=true → is_auth_valid() 返回 False。"""
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=SHORT_TOKEN, auth_required=True,
    )
    assert config.is_auth_valid() is False


def test_config_is_auth_valid_no_alpha_or_digit():
    """纯字母 token（无数字）和纯数字 token（无字母）→ is_auth_valid() 返回 False。"""
    alpha_cfg = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=ALPHA_ONLY, auth_required=True,
    )
    assert alpha_cfg.is_auth_valid() is False

    digit_cfg = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=DIGIT_ONLY, auth_required=True,
    )
    assert digit_cfg.is_auth_valid() is False


def test_config_is_auth_valid_disabled_skips():
    """auth_required=False 时弱 token 也返回 True（认证关闭，token 被忽略）。"""
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token=SHORT_TOKEN, auth_required=False,
    )
    assert config.is_auth_valid() is True


def test_config_is_auth_valid_empty_when_required():
    """required=true 但 token 空 → is_auth_valid() 返回 False。"""
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token="", auth_required=True,
    )
    assert config.is_auth_valid() is False


def test_config_validate_auth_raises_on_invalid():
    """validate_auth() 对无效配置抛 ValueError（覆盖空/短/纯字母/纯数字四种情况）。"""
    # 空 token
    with pytest.raises(ValueError):
        Config(
            username="u", password="p", ip="1.2.3.4", port=3021,
            auth_token="", auth_required=True,
        ).validate_auth()
    # 短 token
    with pytest.raises(ValueError):
        Config(
            username="u", password="p", ip="1.2.3.4", port=3021,
            auth_token=SHORT_TOKEN, auth_required=True,
        ).validate_auth()
    # 纯字母
    with pytest.raises(ValueError):
        Config(
            username="u", password="p", ip="1.2.3.4", port=3021,
            auth_token=ALPHA_ONLY, auth_required=True,
        ).validate_auth()
    # 纯数字
    with pytest.raises(ValueError):
        Config(
            username="u", password="p", ip="1.2.3.4", port=3021,
            auth_token=DIGIT_ONLY, auth_required=True,
        ).validate_auth()


# ========== /health auth 字段测试 ==========

def test_health_status_includes_auth_field():
    """/health 响应含 auth 字段，值为 configured/disabled/misconfigured 之一。"""
    # auth_required=True + 有效 token → configured
    client = make_auth_app(auth_required=True, auth_token=VALID_TOKEN)
    resp = client.get("/health")
    assert resp.status_code != 401
    body = resp.json()
    assert body["auth"] in ("configured", "disabled", "misconfigured")
    assert body["auth"] == "configured"

    # auth_required=False → disabled
    client_off = make_auth_app(auth_required=False, auth_token="")
    resp_off = client_off.get("/health")
    assert resp_off.json()["auth"] == "disabled"


# ========== lifespan 启动校验测试 ==========

def test_lifespan_fails_on_invalid_auth_config():
    """启动时弱 token（auth_required=true）→ with TestClient(app) 抛 ValueError。

    lifespan 在 TestClient 进入 with 块时触发 startup，validate_auth() 失败
    抛 ValueError 冒泡到 with 语句。注意：不进入 with 块不触发校验（create_app 本身不校验）。
    """
    config = Config(
        username="u", password="p", ip="1.2.3.4", port=3021,
        auth_token="short", auth_required=True,  # len=5, 太短
    )
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gw)
    with pytest.raises(ValueError):
        with TestClient(app):
            pass  # 不应到达此处

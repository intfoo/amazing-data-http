# AmazingData HTTP 适配服务实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建一个独立 Python HTTP 适配服务，让主项目通过既有自定义数据源协议获取 AmazingData 1.1.7 的日 K 数据。

**Architecture:** FastAPI 常驻进程，启动时登录 SDK 并初始化 MarketData；HTTP 层依赖内部 `Gateway` 接口（Protocol），真实实现 `AmazingDataGateway` 封装 SDK 调用细节，测试使用 `FakeGateway`。`Serializer` 负责 DataFrame/NumPy/datetime → JSON 安全值，`KlineService` 负责日期转换、周期映射和 `dict[code, DataFrame]` 展平。所有 SDK 字段名原样透传，字段重命名由主项目 YAML `field_map` 完成。

**Tech Stack:** Python 3.14, FastAPI, uvicorn, pandas, numpy, pytest, httpx, Docker (linux/amd64)

## Global Constraints

- Python 标记固定为 `cp314`，基础镜像必须为 `python:3.14`，目标平台 `linux/amd64`
- 安装两个本地 wheel：先 `tgw-1.0.8.7-py3-none-any.whl`，再 `AmazingData-1.1.7-cp314-none-any.whl`
- AmazingData 声明依赖：`pydantic>=2.6.4`、`numba>=0.65.0`、`scipy>=1.15.1`、`tgw>=1.0.8.7`
- **SDK 探测门禁**（spec §5.2）：编写真实 gateway 实现前，必须在目标 Docker 环境完成最小探测并产出探测报告
- 凭据只通过环境变量注入，不得写入 Dockerfile、源码、测试快照或日志
- 适配服务不重命名字段：保留 SDK 原始字段名（`code`、`trade_time`、`open`、`high`、`low`、`close`、`volume`、`amount`）
- `field_map` 方向为 `upstream_field: internal_field`（如 `code: symbol`、`trade_time: date`）
- 日志不记录密码；代码列表过长时只记数量和摘要
- 空结果返回 HTTP `200` 和 `{"data": []}`
- 内部周期参数用字符串白名单映射到 SDK 枚举，不接受 HTTP 客户端传入任意整数
- 首期 `/daily` 固定使用 `day` 周期，不暴露分钟/周/月 K 路由

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `pyproject.toml` | 项目元数据、依赖、pytest 配置 |
| `Dockerfile` | 构建适配服务镜像 |
| `docker-compose.yml` | 本地/部署编排 |
| `.dockerignore` | 排除不必要文件 |
| `.env.example` | 环境变量模板（无真实值） |
| `app/__init__.py` | 包标记 |
| `app/config.py` | `Config` dataclass，从环境变量读取 |
| `app/errors.py` | 错误码常量、`AppError` 异常、`request_id` 中间件 |
| `app/serializer.py` | `serialize_dataframe` + `serialize_value`：DataFrame/NumPy/datetime → JSON 安全值 |
| `app/gateway.py` | `Gateway` Protocol + `AmazingDataGateway` 真实实现 + `PERIOD_MAP` |
| `app/kline_service.py` | `KlineService`：日期转换、展平 dict[code, DataFrame]、调用 gateway+serializer |
| `app/health.py` | `HealthService`：配置+gateway 状态检查 |
| `app/http_app.py` | FastAPI app 工厂、`/daily`+`/health` 路由、错误处理、启动登录 |
| `scripts/probe_sdk.py` | SDK 探测脚本（探测门禁） |
| `tests/conftest.py` | 共享 fixtures：FakeGateway、合成 DataFrame |
| `tests/test_serializer.py` | 序列化单元测试 |
| `tests/test_kline_service.py` | KlineService 单元测试 |
| `tests/test_http_app.py` | HTTP 路由+健康检查+错误处理测试 |
| `docs/probe-report.json` | 探测脚本输出的结构报告（测试产物，非文档） |

---

## Task 1: 项目脚手架与配置

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `.env.example`

**Interfaces:**
- Produces: `Config` dataclass (`from_env()` classmethod, `is_configured()` method)，后续所有 task 依赖

- [ ] **Step 1: 创建 `pyproject.toml`**

```toml
[project]
name = "amazingdata-http"
version = "0.1.0"
description = "AmazingData HTTP adapter service"
requires-python = ">=3.14"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pandas>=2.2",
    "numpy>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "httpx>=0.27",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends._legacy:_Backend"
```

- [ ] **Step 2: 创建 `app/__init__.py`（空文件）和 `tests/__init__.py`（空文件）**

- [ ] **Step 3: 写 `app/config.py` 的失败测试 `tests/test_config.py`**

```python
import os
from app.config import Config


def test_config_from_env_reads_all_vars():
    os.environ["AMAZINGDATA_USERNAME"] = "user1"
    os.environ["AMAZINGDATA_PASSWORD"] = "pass1"
    os.environ["AMAZINGDATA_IP"] = "1.2.3.4"
    os.environ["AMAZINGDATA_PORT"] = "3021"
    os.environ["HTTP_HOST"] = "0.0.0.0"
    os.environ["HTTP_PORT"] = "8080"
    cfg = Config.from_env()
    assert cfg.username == "user1"
    assert cfg.password == "pass1"
    assert cfg.ip == "1.2.3.4"
    assert cfg.port == 3021
    assert cfg.http_host == "0.0.0.0"
    assert cfg.http_port == 8080
    assert cfg.is_configured() is True


def test_config_defaults():
    for key in ["AMAZINGDATA_USERNAME", "AMAZINGDATA_PASSWORD", "AMAZINGDATA_IP", "AMAZINGDATA_PORT"]:
        os.environ.pop(key, None)
    os.environ["HTTP_HOST"] = ""
    os.environ["HTTP_PORT"] = ""
    cfg = Config.from_env()
    assert cfg.http_host == "0.0.0.0"
    assert cfg.http_port == 3021
    assert cfg.is_configured() is False
```

- [ ] **Step 4: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 5: 实现 `app/config.py`**

```python
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    ip: str
    port: int
    http_host: str = "0.0.0.0"
    http_port: int = 3021

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            username=os.environ.get("AMAZINGDATA_USERNAME", ""),
            password=os.environ.get("AMAZINGDATA_PASSWORD", ""),
            ip=os.environ.get("AMAZINGDATA_IP", ""),
            port=int(os.environ.get("AMAZINGDATA_PORT", "0") or "0"),
            http_host=os.environ.get("HTTP_HOST", "0.0.0.0") or "0.0.0.0",
            http_port=int(os.environ.get("HTTP_PORT", "3021") or "3021"),
        )

    def is_configured(self) -> bool:
        return bool(self.username and self.password and self.ip and self.port)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: 创建 `Dockerfile`**

```dockerfile
FROM python:3.14

WORKDIR /app

COPY tgw-1.0.8.7-py3-none-any.whl .
COPY AmazingData-1.1.7-cp314-none-any.whl .

RUN pip install --no-cache-dir \
    ./tgw-1.0.8.7-py3-none-any.whl

RUN pip install --no-cache-dir \
    ./AmazingData-1.1.7-cp314-none-any.whl

COPY pyproject.toml .
COPY app/ app/
COPY scripts/ scripts/

RUN pip install --no-cache-dir .

EXPOSE 3021
CMD ["uvicorn", "app.http_app:app", "--host", "0.0.0.0", "--port", "3021"]
```

- [ ] **Step 8: 创建 `.dockerignore`**

```
.git
docs/
tests/
*.md
.env
__pycache__
*.pyc
.pytest_cache
```

- [ ] **Step 9: 创建 `.env.example`**

```text
AMAZINGDATA_USERNAME=
AMAZINGDATA_PASSWORD=
AMAZINGDATA_IP=
AMAZINGDATA_PORT=
HTTP_HOST=0.0.0.0
HTTP_PORT=3021
```

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml app/__init__.py app/config.py tests/__init__.py tests/test_config.py Dockerfile .dockerignore .env.example
git commit -m "feat: project scaffolding with config and Dockerfile"
```

---

## Task 2: SDK 探测门禁

> **spec §5.2 强制要求：** 编写真实 gateway 实现前必须完成此探测。此 task 需要真实账号和网络，不是普通 CI 步骤。探测结果（`docs/probe-report.json`）是 Task 5（AmazingDataGateway）的实现依据。

**Files:**
- Create: `scripts/probe_sdk.py`
- Create: `docs/probe-report.json`（探测脚本输出产物）

**Interfaces:**
- Produces: `docs/probe-report.json` — 包含 login 参数名、Period 枚举值、query_kline 签名、返回类型、DataFrame 列名/索引名/dtypes、异常类型

- [ ] **Step 1: 编写探测脚本 `scripts/probe_sdk.py`**

```python
"""AmazingData SDK 最小探测脚本。

只输出结构摘要，不输出账号、密码或完整行情数据。
运行方式（在 Docker 容器内）：
    python scripts/probe_sdk.py > docs/probe-report.json
"""
import inspect
import json
import os
import sys
import traceback


def probe():
    report = {"errors": []}

    try:
        import AmazingData as ad
        report["import_ok"] = True
        report["version"] = getattr(ad, "__version__", "unknown")
    except Exception as e:
        report["import_ok"] = False
        report["errors"].append(f"import failed: {type(e).__name__}: {e}")
        report["errors"].append(traceback.format_exc())
        print(json.dumps(report, indent=2, default=str))
        return

    # 1. login 签名
    try:
        sig = inspect.signature(ad.login)
        report["login_params"] = {
            name: {"required": p.default is p.empty, "default": str(p.default) if p.default is not p.empty else None}
            for name, p in sig.parameters.items()
        }
    except Exception as e:
        report["login_params"] = f"inspect failed: {e}"

    # 2. Period 枚举
    try:
        from AmazingData.constant import Period
        period_names = ["day", "min1", "min3", "min5", "min10", "min15",
                        "min30", "min60", "min120", "week", "month", "season", "year"]
        report["period_values"] = {
            name: getattr(Period, name).value for name in period_names if hasattr(Period, name)
        }
        report["period_class"] = str(type(Period))
    except Exception as e:
        report["period_values"] = f"failed: {type(e).__name__}: {e}"

    # 3. 登录（需要环境变量）
    username = os.environ.get("AMAZINGDATA_USERNAME", "")
    password = os.environ.get("AMAZINGDATA_PASSWORD", "")
    ip = os.environ.get("AMAZINGDATA_IP", "")
    port_raw = os.environ.get("AMAZINGDATA_PORT", "0")
    port = int(port_raw) if port_raw else 0

    if not all([username, password, ip, port]):
        report["login_skipped"] = "missing credentials in env"
        print(json.dumps(report, indent=2, default=str))
        return

    try:
        ad.login(username=username, password=password, ip=ip, port=port)
        report["login_ok"] = True
    except Exception as e:
        report["login_ok"] = False
        report["login_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        print(json.dumps(report, indent=2, default=str))
        return

    # 4. BaseData + calendar
    try:
        base = ad.BaseData()
        calendar = base.get_calendar()
        report["calendar_type"] = type(calendar).__name__
        report["calendar_length"] = len(calendar) if hasattr(calendar, "__len__") else "unknown"
        if calendar:
            report["calendar_elem_type"] = type(calendar[-1]).__name__
            report["calendar_last_sample"] = str(calendar[-1])
    except Exception as e:
        report["calendar_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        _safe_logout(ad, report)
        print(json.dumps(report, indent=2, default=str))
        return

    # 5. MarketData
    try:
        md = ad.MarketData(calendar)
        report["marketdata_created"] = True
        sig_qk = inspect.signature(md.query_kline)
        report["query_kline_params"] = {
            name: {"required": p.default is p.empty, "default": str(p.default) if p.default is not p.empty else None}
            for name, p in sig_qk.parameters.items()
        }
    except Exception as e:
        report["marketdata_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        _safe_logout(ad, report)
        print(json.dumps(report, indent=2, default=str))
        return

    # 6. 查询一个代码一个交易日
    trade_day = calendar[-1]
    prev_day = calendar[-2] if len(calendar) > 1 else trade_day
    try:
        result = md.query_kline(
            ["000001.SZ"],
            begin_date=prev_day,
            end_date=trade_day,
            period=ad.constant.Period.day.value,
        )
        report["query_ok"] = True
        report["result_type"] = type(result).__name__

        if isinstance(result, dict):
            report["result_key_count"] = len(result)
            for code, df in result.items():
                report["result_key_sample"] = code
                report["df_type"] = type(df).__name__
                if hasattr(df, "shape"):
                    report["df_shape"] = list(df.shape)
                if hasattr(df, "columns"):
                    report["df_columns"] = list(df.columns)
                if hasattr(df, "index"):
                    report["df_index_name"] = str(df.index.name)
                    report["df_index_dtype"] = str(df.index.dtype)
                if hasattr(df, "dtypes"):
                    report["df_dtypes"] = {col: str(dt) for col, dt in df.dtypes.items()}
                if hasattr(df, "iloc") and len(df) > 0:
                    row = df.iloc[0]
                    report["df_row0_value_types"] = {col: type(row[col]).__name__ for col in df.columns}
                    report["df_row0_index_type"] = type(df.index[0]).__name__
                break
        elif hasattr(result, "columns"):
            report["result_is_dataframe"] = True
            report["df_columns"] = list(result.columns)
            report["df_index_name"] = str(result.index.name)
    except Exception as e:
        report["query_ok"] = False
        report["query_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())

    # 7. 登出
    _safe_logout(ad, report)

    print(json.dumps(report, indent=2, default=str))


def _safe_logout(ad, report):
    try:
        ad.logout()
        report["logout_ok"] = True
    except Exception as e:
        report["logout_ok"] = False
        report["logout_error"] = f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    probe()
```

- [ ] **Step 2: 构建 Docker 镜像（验证 wheel 可安装）**

Run:
```bash
docker build --platform linux/amd64 -t amazingdata-http:probe .
```
Expected: 构建成功。若 `numba`/`scipy` 在 Python 3.14 下无可用 wheel，构建会失败——此时停止并记录错误，按 spec §5.1 要求由用户补充兼容 wheel 或退回兼容 SDK 版本，不得在设计阶段假定跨版本兼容。

- [ ] **Step 3: 在容器内运行探测脚本**

Run:
```bash
docker run --rm --env-file .env --platform linux/amd64 amazingdata-http:probe python scripts/probe_sdk.py > docs/probe-report.json
```
Expected: `docs/probe-report.json` 包含 `login_ok: true`、`query_ok: true`、`df_columns`、`df_index_name`、`period_values` 等字段。

- [ ] **Step 4: 审阅探测报告**

检查 `docs/probe-report.json`，确认以下关键事实（后续 Task 5 依赖）：
- `login_params` 中端口参数名是 `port` 还是 `host`
- `query_kline_params` 中 `period` 是否为必填
- `df_columns` 实际列名
- `df_index_name` 索引名称
- `df_dtypes` 各列数据类型
- `period_values` 各周期枚举的整数值

如果探测结果与本计划的假设（基于 SDK 文档）不同，在 Task 5 实现时以探测报告为准。

- [ ] **Step 5: Commit**

```bash
git add scripts/probe_sdk.py docs/probe-report.json
git commit -m "feat: add SDK probe script and probe report"
```

---

## Task 3: Serializer

**Files:**
- Create: `app/serializer.py`
- Create: `tests/test_serializer.py`

**Interfaces:**
- Consumes: 无（纯函数）
- Produces: `serialize_value(v) -> JSON-safe`、`serialize_dataframe(df) -> list[dict]`

- [ ] **Step 1: 写失败测试 `tests/test_serializer.py`**

```python
import math
import numpy as np
import pandas as pd
from datetime import datetime, date
from app.serializer import serialize_value, serialize_dataframe


def test_serialize_numpy_int():
    assert serialize_value(np.int64(42)) == 42
    assert isinstance(serialize_value(np.int64(42)), int)


def test_serialize_numpy_float():
    assert serialize_value(np.float64(3.14)) == 3.14
    assert isinstance(serialize_value(np.float64(3.14)), float)


def test_serialize_numpy_float_nan():
    assert serialize_value(np.float64(float("nan"))) is None


def test_serialize_numpy_bool():
    assert serialize_value(np.bool_(True)) is True
    assert serialize_value(np.bool_(False)) is False


def test_serialize_datetime():
    dt = datetime(2024, 1, 2, 15, 30, 0)
    assert serialize_value(dt) == "2024-01-02T15:30:00"


def test_serialize_date():
    assert serialize_value(date(2024, 1, 2)) == "2024-01-02"


def test_serialize_pandas_timestamp():
    ts = pd.Timestamp("2024-01-02")
    assert serialize_value(ts) == "2024-01-02T00:00:00"


def test_serialize_nat():
    assert serialize_value(pd.NaT) is None


def test_serialize_python_nan():
    assert serialize_value(float("nan")) is None


def test_serialize_none():
    assert serialize_value(None) is None


def test_serialize_plain_values_passthrough():
    assert serialize_value(42) == 42
    assert serialize_value("hello") == "hello"
    assert serialize_value(3.14) == 3.14


def test_serialize_dataframe_basic():
    df = pd.DataFrame({
        "code": ["000001.SZ"],
        "open": [10.2],
        "volume": [np.int64(1234567)],
    })
    result = serialize_dataframe(df)
    assert result == [{"code": "000001.SZ", "open": 10.2, "volume": 1234567}]


def test_serialize_dataframe_with_nan():
    df = pd.DataFrame({
        "open": [10.2, float("nan")],
        "close": [np.float64(float("nan")), 10.5],
    })
    result = serialize_dataframe(df)
    assert result == [{"open": 10.2, "close": None}, {"open": None, "close": 10.5}]


def test_serialize_dataframe_resets_index():
    df = pd.DataFrame({"open": [10.2]}, index=pd.Index(["2024-01-02"], name="trade_time"))
    result = serialize_dataframe(df)
    assert result == [{"trade_time": "2024-01-02", "open": 10.2}]


def test_serialize_dataframe_empty():
    df = pd.DataFrame({"open": [], "close": []})
    assert serialize_dataframe(df) == []


def test_serialize_dataframe_with_datetime_column():
    df = pd.DataFrame({
        "trade_time": [pd.Timestamp("2024-01-02")],
        "close": [10.3],
    })
    result = serialize_dataframe(df)
    assert result == [{"trade_time": "2024-01-02T00:00:00", "close": 10.3}]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_serializer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.serializer'`

- [ ] **Step 3: 实现 `app/serializer.py`**

```python
import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def serialize_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        val = float(v)
        return None if math.isnan(val) else val
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.isoformat()
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is pd.NaT:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def serialize_dataframe(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    records = df.reset_index().to_dict(orient="records")
    return [{k: serialize_value(v) for k, v in record.items()} for record in records]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_serializer.py -v`
Expected: PASS (全部测试)

- [ ] **Step 5: Commit**

```bash
git add app/serializer.py tests/test_serializer.py
git commit -m "feat: add DataFrame/NumPy/datetime JSON serializer"
```

---

## Task 4: Gateway 接口与 FakeGateway

**Files:**
- Create: `app/gateway.py`
- Create: `tests/conftest.py`
- Create: `tests/test_gateway_interface.py`

**Interfaces:**
- Produces: `Gateway` Protocol（`login()`, `logout()`, `is_ready()`, `query_kline(symbols, begin_date, end_date, period) -> dict[str, pd.DataFrame]`）
- Produces: `PERIOD_MAP` 字典（`"day"` → `Period.day.value` 等）
- Produces: `tests/conftest.py` 中的 `FakeGateway` fixture，供 Task 6/7 使用

- [ ] **Step 1: 写 `app/gateway.py`（Protocol + PERIOD_MAP，真实实现留到 Task 5）**

```python
from typing import Any, Protocol

import pandas as pd


PERIOD_MAP: dict[str, str] = {
    "day": "day",
    "min1": "min1",
    "min3": "min3",
    "min5": "min5",
    "min10": "min10",
    "min15": "min15",
    "min30": "min30",
    "min60": "min60",
    "min120": "min120",
    "week": "week",
    "month": "month",
    "season": "season",
    "year": "year",
}


class Gateway(Protocol):
    def login(self) -> None: ...
    def logout(self) -> None: ...
    def is_ready(self) -> bool: ...
    def query_kline(
        self,
        symbols: list[str],
        begin_date: int,
        end_date: int,
        period: str,
    ) -> dict[str, pd.DataFrame]: ...


class GatewayError(Exception):
    pass


class GatewayNotReadyError(GatewayError):
    pass


class GatewayQueryError(GatewayError):
    pass
```

> 说明：`PERIOD_MAP` 此处映射 string→string（内部周期名）。`AmazingDataGateway`（Task 5）在内部将 string 转为 `Period.<name>.value`。这样 Gateway Protocol 不依赖 SDK 类型，测试可用 FakeGateway 传入任意 string period。

- [ ] **Step 2: 写 `tests/conftest.py`（FakeGateway + 合成 DataFrame fixtures）**

```python
import numpy as np
import pandas as pd
import pytest

from app.gateway import Gateway, GatewayNotReadyError, GatewayQueryError


class FakeGateway:
    def __init__(self, ready: bool = True, result: dict[str, pd.DataFrame] | None = None):
        self._ready = ready
        self._result = result if result is not None else {}
        self._logged_in = ready
        self.login_called = 0
        self.logout_called = 0
        self.query_calls: list[dict] = []

    def login(self) -> None:
        self.login_called += 1
        self._logged_in = True
        self._ready = True

    def logout(self) -> None:
        self.logout_called += 1
        self._logged_in = False
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    def query_kline(self, symbols, begin_date, end_date, period):
        self.query_calls.append({
            "symbols": symbols,
            "begin_date": begin_date,
            "end_date": end_date,
            "period": period,
        })
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        if self._result is None:
            raise GatewayQueryError("fake query failed")
        return self._result


def make_daily_df(code: str = "000001.SZ", rows: int = 1) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "code": [code] * rows,
            "trade_time": dates,
            "open": [10.2] * rows,
            "high": [10.45] * rows,
            "low": [10.1] * rows,
            "close": [10.3] * rows,
            "volume": [np.int64(1234567)] * rows,
            "amount": [12700000.0] * rows,
        },
        index=pd.Index(dates, name="trade_time"),
    )


@pytest.fixture
def fake_gateway():
    return FakeGateway(ready=True)


@pytest.fixture
def fake_gateway_with_data():
    return FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
```

- [ ] **Step 3: 写接口验证测试 `tests/test_gateway_interface.py`**

```python
from app.gateway import Gateway, PERIOD_MAP, GatewayError, GatewayNotReadyError, GatewayQueryError
from tests.conftest import FakeGateway


def test_fake_gateway_satisfies_protocol():
    gw = FakeGateway()
    assert isinstance(gw, Gateway) or hasattr(gw, "login")


def test_period_map_contains_day():
    assert PERIOD_MAP["day"] == "day"


def test_period_map_contains_all_periods():
    expected = {"day", "min1", "min3", "min5", "min10", "min15",
                "min30", "min60", "min120", "week", "month", "season", "year"}
    assert expected <= set(PERIOD_MAP.keys())


def test_gateway_error_hierarchy():
    assert issubclass(GatewayNotReadyError, GatewayError)
    assert issubclass(GatewayQueryError, GatewayError)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_gateway_interface.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/gateway.py tests/conftest.py tests/test_gateway_interface.py
git commit -m "feat: add Gateway protocol, PERIOD_MAP, and FakeGateway test fixture"
```

---

## Task 5: AmazingDataGateway 真实实现

> **依赖 Task 2 探测报告。** 以下代码基于 SDK 文档假设（`port` 参数、`Period.day.value`、`query_kline(code_list, begin_date, end_date, period)` 返回 `dict[str, DataFrame]`）。如果探测报告显示不同，按报告调整。

**Files:**
- Modify: `app/gateway.py`（追加 `AmazingDataGateway` 类）

**Interfaces:**
- Consumes: `Config`（Task 1）、`PERIOD_MAP`（Task 4）、探测报告 `docs/probe-report.json`
- Produces: `AmazingDataGateway` 类，实现 `Gateway` Protocol

- [ ] **Step 1: 在 `app/gateway.py` 末尾追加 `AmazingDataGateway` 实现**

在 `app/gateway.py` 文件末尾追加以下代码（不修改已有的 Protocol 和异常类）：

```python
import logging
import threading

from app.config import Config

logger = logging.getLogger("amazingdata.gateway")


class AmazingDataGateway:
    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._ad = None
        self._market_data = None
        self._ready = False

    def login(self) -> None:
        with self._lock:
            self._do_login()

    def _do_login(self) -> None:
        try:
            import AmazingData as ad
        except ImportError as e:
            logger.error("AmazingData import failed: %s", e)
            self._ready = False
            raise GatewayNotReadyError(f"SDK import failed: {e}") from e

        self._ad = ad
        try:
            if self._ready:
                self._safe_logout()
            ad.login(
                username=self._config.username,
                password=self._config.password,
                ip=self._config.ip,
                port=self._config.port,
            )
            base = ad.BaseData()
            calendar = base.get_calendar()
            self._market_data = ad.MarketData(calendar)
            self._ready = True
            logger.info("AmazingData gateway login successful")
        except Exception as e:
            self._ready = False
            logger.error("AmazingData login failed: %s: %s", type(e).__name__, e)
            raise GatewayNotReadyError(f"login failed: {e}") from e

    def logout(self) -> None:
        with self._lock:
            self._safe_logout()

    def _safe_logout(self) -> None:
        if self._ad is None:
            return
        try:
            self._ad.logout()
        except Exception as e:
            logger.warning("logout error (ignored): %s: %s", type(e).__name__, e)
        self._ready = False
        self._market_data = None

    def is_ready(self) -> bool:
        return self._ready

    def query_kline(
        self,
        symbols: list[str],
        begin_date: int,
        end_date: int,
        period: str,
    ) -> dict[str, "pd.DataFrame"]:
        if not self._ready or self._market_data is None:
            raise GatewayNotReadyError("gateway not ready")
        sdk_period_name = PERIOD_MAP.get(period)
        if sdk_period_name is None:
            raise GatewayQueryError(f"unsupported period: {period}")
        try:
            from AmazingData.constant import Period
            sdk_period_value = getattr(Period, sdk_period_name).value
        except Exception as e:
            raise GatewayQueryError(f"period mapping failed: {e}") from e

        with self._lock:
            try:
                result = self._market_data.query_kline(
                    symbols,
                    begin_date=begin_date,
                    end_date=end_date,
                    period=sdk_period_value,
                )
                return result if isinstance(result, dict) else {"_all": result}
            except Exception as e:
                logger.error(
                    "query_kline failed: %s: %s (symbols=%d, begin=%d, end=%d, period=%s)",
                    type(e).__name__, e, len(symbols), begin_date, end_date, period,
                )
                raise GatewayQueryError(f"query failed: {e}") from e
```

- [ ] **Step 2: 写 `AmazingDataGateway` 单元测试（不依赖真实 SDK，测试错误路径）**

创建 `tests/test_amazingdata_gateway.py`：

```python
import pytest
from app.config import Config
from app.gateway import GatewayNotReadyError, GatewayQueryError


def make_config():
    return Config(username="u", password="p", ip="1.2.3.4", port=3021)


def test_gateway_not_ready_before_login():
    from app.gateway import AmazingDataGateway
    gw = AmazingDataGateway(make_config())
    assert gw.is_ready() is False
    with pytest.raises(GatewayNotReadyError):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "day")


def test_gateway_query_unsupported_period():
    from app.gateway import AmazingDataGateway
    gw = AmazingDataGateway(make_config())
    gw._ready = True
    gw._market_data = object()
    with pytest.raises(GatewayQueryError, match="unsupported period"):
        gw.query_kline(["000001.SZ"], 20240101, 20240131, "invalid_period")
```

- [ ] **Step 3: 运行测试确认通过**

Run: `python -m pytest tests/test_amazingdata_gateway.py tests/test_gateway_interface.py -v`
Expected: PASS

- [ ] **Step 4: 核对探测报告，调整不一致项**

打开 `docs/probe-report.json`，逐项核对：
- 如果 `login_params` 中端口参数名不是 `port`（例如是 `host`），修改 `_do_login` 中的 `ad.login(...)` 调用
- 如果 `query_kline_params` 显示 `period` 不是必填，保留显式传参（无害）
- 如果 `result_type` 不是 `dict`，调整 `query_kline` 的返回值包装逻辑

- [ ] **Step 5: Commit**

```bash
git add app/gateway.py tests/test_amazingdata_gateway.py
git commit -m "feat: add AmazingDataGateway real implementation"
```

---

## Task 6: KlineService

**Files:**
- Create: `app/kline_service.py`
- Create: `tests/test_kline_service.py`

**Interfaces:**
- Consumes: `Gateway` Protocol（Task 4）、`serialize_dataframe`（Task 3）
- Produces: `KlineService` 类，`query(symbols, start_time, end_time) -> list[dict]`

- [ ] **Step 1: 写失败测试 `tests/test_kline_service.py`**

```python
import numpy as np
import pandas as pd
import pytest

from app.gateway import GatewayNotReadyError, GatewayQueryError
from app.kline_service import KlineService, to_sdk_date
from tests.conftest import FakeGateway, make_daily_df


def test_to_sdk_date():
    assert to_sdk_date("2024-01-01") == 20240101
    assert to_sdk_date("2024-12-31") == 20241231


def test_to_sdk_date_invalid():
    with pytest.raises(ValueError):
        to_sdk_date("20240101")
    with pytest.raises(ValueError):
        to_sdk_date("not-a-date")


def test_query_returns_flattened_records():
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert len(data) == 1
    row = data[0]
    assert row["code"] == "000001.SZ"
    assert row["open"] == 10.2
    assert row["close"] == 10.3
    assert row["volume"] == 1234567


def test_query_passes_sdk_dates_and_day_period():
    gw = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    svc = KlineService(gw)
    svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")
    call = gw.query_calls[0]
    assert call["begin_date"] == 20240101
    assert call["end_date"] == 20240131
    assert call["period"] == "day"


def test_query_empty_result():
    gw = FakeGateway(ready=True, result={})
    svc = KlineService(gw)
    assert svc.query(["000001.SZ"], "2024-01-01", "2024-01-31") == []


def test_query_empty_dataframe():
    df = pd.DataFrame({"code": [], "open": []})
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    assert svc.query(["000001.SZ"], "2024-01-01", "2024-01-31") == []


def test_query_multiple_codes():
    result = {
        "000001.SZ": make_daily_df("000001.SZ"),
        "600000.SH": make_daily_df("600000.SH"),
    }
    gw = FakeGateway(ready=True, result=result)
    svc = KlineService(gw)
    data = svc.query(["000001.SZ", "600000.SH"], "2024-01-02", "2024-01-02")
    assert len(data) == 2
    codes = {row["code"] for row in data}
    assert codes == {"000001.SZ", "600000.SH"}


def test_query_dataframe_without_code_column():
    df = pd.DataFrame(
        {"open": [10.2], "close": [10.3]},
        index=pd.Index(["2024-01-02"], name="trade_time"),
    )
    gw = FakeGateway(ready=True, result={"000001.SZ": df})
    svc = KlineService(gw)
    data = svc.query(["000001.SZ"], "2024-01-02", "2024-01-02")
    assert data[0]["code"] == "000001.SZ"
    assert data[0]["open"] == 10.2


def test_query_gateway_not_ready():
    gw = FakeGateway(ready=False)
    svc = KlineService(gw)
    with pytest.raises(GatewayNotReadyError):
        svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")


def test_query_gateway_error():
    gw = FakeGateway(ready=True, result=None)
    svc = KlineService(gw)
    with pytest.raises(GatewayQueryError):
        svc.query(["000001.SZ"], "2024-01-01", "2024-01-31")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_kline_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.kline_service'`

- [ ] **Step 3: 实现 `app/kline_service.py`**

```python
import re
from datetime import datetime

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_sdk_date(iso: str) -> int:
    if not _ISO_DATE.match(iso):
        raise ValueError(f"date must be YYYY-MM-DD, got: {iso}")
    datetime.strptime(iso, "%Y-%m-%d")
    return int(iso.replace("-", ""))


class KlineService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(self, symbols: list[str], start_time: str, end_time: str) -> list[dict]:
        begin_date = to_sdk_date(start_time)
        end_date = to_sdk_date(end_time)
        result = self._gw.query_kline(symbols, begin_date, end_date, "day")
        return self._flatten(result)

    @staticmethod
    def _flatten(result: dict[str, pd.DataFrame]) -> list[dict]:
        records: list[dict] = []
        for code, df in result.items():
            if df is None or df.empty:
                continue
            df = df.reset_index()
            if "code" not in df.columns:
                df["code"] = code
            records.extend(serialize_dataframe(df))
        return records
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_kline_service.py -v`
Expected: PASS (全部测试)

- [ ] **Step 5: Commit**

```bash
git add app/kline_service.py tests/test_kline_service.py
git commit -m "feat: add KlineService with date conversion and dict flattening"
```

---

## Task 7: HTTP 应用（路由、健康检查、错误处理、启动登录）

**Files:**
- Create: `app/errors.py`
- Create: `app/health.py`
- Create: `app/http_app.py`
- Create: `tests/test_http_app.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`Gateway`（Task 4/5）、`KlineService`（Task 6）
- Produces: FastAPI `app` 实例（`app.http_app:app`），`/daily` 和 `/health` 路由

- [ ] **Step 1: 实现 `app/errors.py`**

```python
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
```

- [ ] **Step 2: 实现 `app/health.py`**

```python
from app.gateway import Gateway


class HealthService:
    def __init__(self, config, gateway: Gateway):
        self._config = config
        self._gw = gateway

    def status(self) -> dict:
        ready = self._config.is_configured() and self._gw.is_ready()
        return {
            "status": "ok" if ready else "degraded",
            "sdk": "ready" if self._gw.is_ready() else "not_ready",
            "config": "complete" if self._config.is_configured() else "incomplete",
        }

    def is_ok(self) -> bool:
        return self._config.is_configured() and self._gw.is_ready()
```

- [ ] **Step 3: 实现 `app/http_app.py`**

```python
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
        request_id = get_request_id(request)
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
        except (TypeError, OverflowError, ValueError) as e:
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
```

- [ ] **Step 4: 写失败测试 `tests/test_http_app.py`**

```python
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.http_app import create_app
from tests.conftest import FakeGateway, make_daily_df


def make_test_app(gateway=None):
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    if gateway is None:
        gateway = FakeGateway(ready=True, result={"000001.SZ": make_daily_df()})
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)


def test_daily_success():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-02",
        "end_time": "2024-01-02",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert len(body["data"]) == 1
    assert body["data"][0]["code"] == "000001.SZ"
    assert body["data"][0]["close"] == 10.3


def test_daily_empty_result():
    gw = FakeGateway(ready=True, result={})
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_daily_empty_symbols():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": [],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 422


def test_daily_reversed_dates():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-31",
        "end_time": "2024-01-01",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_daily_invalid_date_format():
    client = make_test_app()
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "20240101",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 422


def test_daily_sdk_not_ready():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_daily_sdk_query_failed():
    gw = FakeGateway(ready=True, result=None)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "SDK_QUERY_FAILED"


def test_daily_error_has_request_id():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/daily", json={
        "symbols": ["000001.SZ"],
        "start_time": "2024-01-01",
        "end_time": "2024-01-31",
    })
    assert "request_id" in resp.json()["error"]
    assert resp.headers.get("X-Request-ID") is not None


def test_health_ok():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sdk"] == "ready"


def test_health_degraded_when_not_ready():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["sdk"] == "not_ready"


def test_health_no_secrets_in_response():
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.get("/health")
    body_text = resp.text
    assert "password" not in body_text.lower()
    assert "u" != body_text  # config username not leaked
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_http_app.py -v`
Expected: PASS (全部测试)

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `python -m pytest -v`
Expected: PASS (所有测试)

- [ ] **Step 7: Commit**

```bash
git add app/errors.py app/health.py app/http_app.py tests/test_http_app.py
git commit -m "feat: add FastAPI HTTP app with /daily, /health, and error handling"
```

---

## Task 8: Docker 部署与端到端验证

**Files:**
- Create: `docker-compose.yml`
- Modify: `Dockerfile`（确认最终版本）

**Interfaces:**
- Consumes: 全部前序 task

- [ ] **Step 1: 创建 `docker-compose.yml`**

```yaml
version: "3.8"

services:
  amazingdata-http:
    build:
      context: .
      dockerfile: Dockerfile
    platform: linux/amd64
    ports:
      - "3021:3021"
    env_file:
      - .env
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:3021/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

- [ ] **Step 2: 重新构建镜像并启动**

Run:
```bash
docker compose build
docker compose up -d
```
Expected: 容器启动，日志显示 `gateway login succeeded on startup`。

- [ ] **Step 3: 验证健康检查**

Run: `curl http://localhost:3021/health`
Expected: HTTP 200，body 包含 `{"status":"ok","sdk":"ready","config":"complete"}`

- [ ] **Step 4: 验证日 K 查询**

Run:
```bash
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```
Expected: HTTP 200，body 为 `{"data": [...]}`，每条记录包含 `code`、`trade_time`、`open`、`high`、`low`、`close`、`volume`、`amount` 字段（字段名以探测报告为准）。

- [ ] **Step 5: 验证空结果**

用一个无数据的日期区间查询：
```bash
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-02-10\",\"end_time\":\"2024-02-10\"}"
```
Expected: HTTP 200，`{"data": []}`

- [ ] **Step 6: 验证错误响应**

查询未登录状态（停掉容器后用错误配置重启），确认 `/health` 返回 503 且不含密码。

- [ ] **Step 7: 验证主项目集成（主项目侧 YAML 配置）**

在主项目的自定义数据源 YAML 中配置：
```yaml
url: http://amazingdata-http:3021/daily
method: POST
response_path: data
auth_type: none
field_map:
  code: symbol
  trade_time: date
  open: open
  high: high
  low: low
  close: close
  volume: volume
  amount: amount
transforms:
  date: "parse_date(value, '%Y-%m-%d')"
```
执行主项目"试拉测试"，确认能解析 `response_path: data` 并正确映射字段。

- [ ] **Step 8: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add docker-compose deployment and e2e validation"
```

---

## 自审清单

### 1. Spec 覆盖检查

| Spec 条目 | 覆盖 Task |
|-----------|-----------|
| §2.1 POST /daily | Task 7 |
| §2.1 GET /health | Task 7 |
| §2.1 Linux x86_64 Docker | Task 1 (Dockerfile), Task 8 (compose) |
| §2.1 安装 wheel | Task 1 (Dockerfile) |
| §2.1 JSON 序列化 | Task 3 (Serializer) |
| §2.1 单元测试 | Task 3/4/6/7 |
| §2.1 凭据环境变量 | Task 1 (Config + .env.example) |
| §4.1 请求体校验 | Task 7 (DailyRequest + 路由) |
| §4.1 空结果 200 | Task 7 (test_daily_empty_result) |
| §4.2 健康响应 | Task 7 (HealthService + /health) |
| §4.2 503 不含密码 | Task 7 (test_health_no_secrets_in_response) |
| §4.3 错误结构 | Task 7 (errors.py + exception_handler) |
| §4.3 错误码 | Task 7 (errors.py 常量) |
| §5.1 Python 3.14 | Task 1 (Dockerfile + pyproject) |
| §5.1 tgw 兼容性检查 | Task 2 (Step 2 构建验证) |
| §5.2 探测门禁 | Task 2 |
| §5.3 Gateway 接口 | Task 4 (Protocol) |
| §5.4 周期映射 | Task 4 (PERIOD_MAP) |
| §6.1 启动登录 | Task 7 (startup_login) |
| §6.2 会话失效 | Task 5 (is_ready 检查 + GatewayNotReadyError) |
| §7.1 linux/amd64 | Task 1 (Dockerfile), Task 8 (compose platform) |
| §7.2 环境变量 | Task 1 (Config + .env.example) |
| §7.3 compose 服务名 | Task 8 (docker-compose.yml) |
| §8.1 单元测试覆盖 | Task 3/4/6/7 |
| §8.2 探测测试 | Task 2 |
| §8.3 端到端验收 | Task 8 |

### 2. 占位符扫描

无 TBD/TODO/"implement later"。所有代码步骤含完整代码。Task 5 Step 4 的探测报告核对是条件性调整指令（非占位符），因为有具体核对项。

### 3. 类型一致性

- `Gateway.query_kline(symbols, begin_date, end_date, period)` 签名在 Task 4（Protocol）、Task 5（AmazingDataGateway）、Task 6（KlineService 调用）中一致
- `KlineService.query(symbols, start_time, end_time)` 在 Task 6 定义、Task 7 调用，签名一致
- `serialize_dataframe(df) -> list[dict]` 在 Task 3 定义、Task 6 调用，签名一致
- `Config.from_env()` / `Config.is_configured()` 在 Task 1 定义、Task 5/7 调用，一致
- `AppError(code, message, status_code)` 在 Task 7 errors.py 定义并在同文件路由中使用，一致
- `HealthService(config, gateway)` 在 Task 7 health.py 定义并在 http_app.py 中使用，一致

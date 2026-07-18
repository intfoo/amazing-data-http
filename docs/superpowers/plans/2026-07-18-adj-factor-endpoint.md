# /adj_factor 端点 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `POST /adj_factor` HTTP 端点，对接 AmazingData SDK 的 `BaseData.get_adj_factor`（手册 3.5.2.6 单次复权因子），返回每次除权除息事件的单次因子长表。

**Architecture:** 三层（与现有 `/daily` 同构）—— 路由层 `http_app.py` 负责请求校验/SdkGate 限流/错误映射；服务层 `adj_factor_service.py`（新）负责 SDK 宽表 melt 成长表 + 日期过滤 + 序列化；网关层 `gateway.py` 封装 SDK 调用 + 惰性重连。字段命名中性（`code`/`trade_date`/`adj_factor`），不与 stocker 耦合，stocker 通过自身 YAML `field_map` 适配。

**Tech Stack:** Python 3.13 / FastAPI / Pydantic / pandas / AmazingData SDK / pytest

## Global Constraints

- 字段命名中性：响应字段为 `code`/`trade_date`/`adj_factor`，不得命名为 stocker 的 `symbol`/`ex_factor`（设计原则：接口通用不掺杂 stocker）
- `trade_date` 响应为 `YYYY-MM-DD` 字符串（非 ISO datetime）
- SDK `get_adj_factor` 签名：`(code_list, local_path, is_local)`，**无 begin_date/end_date 参数**；`local_path` 必须为绝对路径（手册 3.5.2.6 注(1)）
- `is_local=False` 时 SDK 仍会写 `local_path` 缓存（手册注(2)），目录必须可写
- 错误码复用现有 `app/errors.py` 常量（INVALID_REQUEST/SDK_NOT_READY/SDK_QUERY_FAILED/SERVICE_BUSY/INTERNAL_ERROR/SERIALIZATION_FAILED）
- 测试用 `FakeGateway`（`tests/conftest.py`）+ `TestClient`，参考 `test_http_app.py` 风格
- Windows PowerShell 环境 jest/pytest 执行铁律：禁用管道重定向吞输出，用 `node` 直跑或 pytest 直跑落盘
- 工作区绝对路径：`d:\_yz\stocker\amazingDataHttp`

---

## File Structure

| 文件 | 职责 | 操作 |
|---|---|---|
| `app/config.py` | 新增 `adj_factor_local_path` 配置字段 | Modify |
| `app/gateway.py` | `Gateway` Protocol 加 `get_adj_factor`；`AmazingDataGateway` 实现 | Modify |
| `app/adj_factor_service.py` | SDK 宽表 melt + 日期过滤 + 序列化 | Create |
| `app/http_app.py` | `AdjFactorRequest` 模型 + `POST /adj_factor` 路由 + 实例化 service | Modify |
| `tests/conftest.py` | `FakeGateway.get_adj_factor` + `make_adj_factor_df` 工厂 | Modify |
| `tests/test_adj_factor.py` | service + HTTP 端到端测试 | Create |
| `tests/test_config.py` | adj_factor_local_path 配置测试 | Modify |

---

### Task 1: Config 扩展（adj_factor_local_path）

**Files:**
- Modify: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.adj_factor_local_path: str`（默认 `""`），`Config.from_env()` 读取 `ADJ_FACTOR_LOCAL_PATH` 环境变量

- [ ] **Step 1: Write failing test** — 追加到 `tests/test_config.py` 末尾

```python
def test_config_default_adj_factor_local_path(monkeypatch):
    monkeypatch.delenv("ADJ_FACTOR_LOCAL_PATH", raising=False)
    cfg = Config.from_env()
    assert cfg.adj_factor_local_path == ""


def test_config_reads_adj_factor_local_path(monkeypatch):
    monkeypatch.setenv("ADJ_FACTOR_LOCAL_PATH", "D://cache//adj//")
    cfg = Config.from_env()
    assert cfg.adj_factor_local_path == "D://cache//adj//"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_config_default_adj_factor_local_path -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'adj_factor_local_path'`

- [ ] **Step 3: Implement** — 修改 `app/config.py`

在 `Config` dataclass 的 `sdk_max_concurrent` 字段后新增字段：

```python
    sdk_max_concurrent: int = 5  # SDK 最大并发调用数，超出返回 503
    adj_factor_local_path: str = ""  # SDK get_adj_factor 的 local_path 参数，必须为绝对路径
```

在 `from_env` 的 return cls(...) 末尾新增一行（在 `sdk_max_concurrent=...` 之后）：

```python
            adj_factor_local_path=os.environ.get("ADJ_FACTOR_LOCAL_PATH", "") or "",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS（含两个新测试 + 原有测试全过）

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat(config): add adj_factor_local_path for SDK get_adj_factor"
```

---

### Task 2: Gateway 层（Protocol + AmazingDataGateway + FakeGateway + 工厂）

**Files:**
- Modify: `app/gateway.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_gateway_interface.py`（验证 Protocol isinstance）

**Interfaces:**
- Consumes: `Config.adj_factor_local_path`（Task 1）
- Produces: `Gateway.get_adj_factor(codes: list[str]) -> pd.DataFrame`（Protocol 方法）；`FakeGateway.get_adj_factor`；`make_adj_factor_df()` 工厂

- [ ] **Step 1: Write failing test** — 追加到 `tests/test_gateway_interface.py` 末尾

```python
def test_fake_gateway_implements_get_adj_factor():
    """FakeGateway 扩展 get_adj_factor 后仍满足 Gateway Protocol（@runtime_checkable）。"""
    from tests.conftest import FakeGateway, make_adj_factor_df
    from app.gateway import Gateway
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    assert isinstance(gw, Gateway)  # Protocol runtime check
    df = gw.get_adj_factor(["000001.SZ"])
    assert df is not None
    assert not df.empty


def test_fake_gateway_get_adj_factor_not_ready():
    from tests.conftest import FakeGateway
    from app.gateway import GatewayNotReadyError
    gw = FakeGateway(ready=False)
    import pytest
    with pytest.raises(GatewayNotReadyError):
        gw.get_adj_factor(["000001.SZ"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gateway_interface.py::test_fake_gateway_implements_get_adj_factor -v`
Expected: FAIL with `TypeError: get_adj_factor() missing` 或 `AttributeError`

- [ ] **Step 3: Modify `app/gateway.py`** — Protocol + AmazingDataGateway

在 `Gateway` Protocol（`stop_subscription` 方法后）新增方法声明：

```python
    def get_adj_factor(self, codes: list[str]) -> "pd.DataFrame": ...
```

在 `AmazingDataGateway` 类（`stop_subscription` 方法后）新增实现：

```python
    def get_adj_factor(self, codes: list[str]) -> "pd.DataFrame":
        """获取单次复权因子（手册 3.5.2.6）。返回 SDK 原始 DataFrame（宽表：index=交易日期, columns=股票代码）。

        SDK 签名 get_adj_factor(code_list, local_path, is_local)，无日期参数。
        is_local=False：从服务端取最新，但仍会更新 local_path 缓存（手册注(2)）。
        local_path 必须为绝对路径，未配置时抛 GatewayNotReadyError → HTTP 503。
        """
        if not self._ready or self._base_data is None:
            raise GatewayNotReadyError("gateway not ready")
        if not self._config.adj_factor_local_path:
            raise GatewayNotReadyError("adj_factor_local_path not configured")
        with self._lock:
            try:
                return self._base_data.get_adj_factor(
                    codes,
                    local_path=self._config.adj_factor_local_path,
                    is_local=False,
                )
            except Exception as e:
                logger.error("get_adj_factor failed: %s: %s (codes=%d)",
                             type(e).__name__, e, len(codes))
                if _is_connection_error(e):
                    logger.warning("get_adj_factor connection error, attempting relogin: %s", e)
                    try:
                        self._do_login()
                        result = self._base_data.get_adj_factor(
                            codes,
                            local_path=self._config.adj_factor_local_path,
                            is_local=False,
                        )
                        logger.info("get_adj_factor succeeded after relogin")
                        return result
                    except Exception as e2:
                        logger.error("get_adj_factor failed after reconnect: %s: %s",
                                     type(e2).__name__, e2)
                        raise GatewayQueryError(f"get_adj_factor failed after reconnect: {e2}") from e2
                raise GatewayQueryError(f"get_adj_factor failed: {e}") from e
```

- [ ] **Step 4: Modify `tests/conftest.py`** — FakeGateway + 工厂

在 `FakeGateway.__init__` 的 `self._sub_code_list = None` 后新增（构造参数 + 调用记录）：

```python
    def __init__(self, ready: bool = True, result: dict[str, pd.DataFrame] | None = _UNSET,
                 adj_factor_result: pd.DataFrame | None = None):
        self._ready = ready
        self._result = result if result is not _UNSET else {}
        self._adj_factor_result = adj_factor_result
        self._logged_in = ready
        self.login_called = 0
        self.logout_called = 0
        self.query_calls: list[dict] = []
        self.adj_factor_query_calls: list[dict] = []
        self._code_list = ["000001.SZ", "600000.SH"]
        self._index_code_list = ["000001.SH", "399001.SZ"]
        self.sub_start_called = 0
        self.sub_stop_called = 0
        self._sub_code_list = None
```

注意：原 `__init__` 签名只有 `ready` 和 `result`，需改为新增 `adj_factor_result` 参数（默认 None）。保留原有字段赋值不变，只新增 `self._adj_factor_result` 和 `self.adj_factor_query_calls`。

在 `FakeGateway.stop_subscription` 方法后新增 `get_adj_factor` 方法：

```python
    def get_adj_factor(self, codes):
        """FakeGateway 除权因子查询：记录调用，未就绪抛 GatewayNotReadyError。"""
        self.adj_factor_query_calls.append({"codes": codes})
        if not self._ready:
            raise GatewayNotReadyError("fake not ready")
        if self._adj_factor_result is None:
            return pd.DataFrame()
        return self._adj_factor_result
```

在 `make_daily_df` 函数后新增 `make_adj_factor_df` 工厂：

```python
def make_adj_factor_df() -> pd.DataFrame:
    """模拟 SDK get_adj_factor 返回的宽表：index=交易日期, columns=股票代码。

    含 NaN 单元格模拟非除权日（dropna 后被过滤）。与 SDK 手册 3.5.2.6 输出格式一致。
    """
    dates = pd.date_range("2024-05-30", periods=2, freq="D")
    return pd.DataFrame(
        {
            "000001.SZ": [1.05, None],   # 5-30 除权, 5-31 无
            "600000.SH": [None, 1.10],    # 5-30 无, 5-31 除权
        },
        index=pd.Index(dates, name="trade_date"),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_gateway_interface.py -v`
Expected: PASS（含两个新测试 + 原有 `test_fake_gateway_get_code_list`）

- [ ] **Step 6: Run full suite to verify no regression**

Run: `python -m pytest tests/ -v --tb=short`
Expected: 全部 PASS（FakeGateway 签名变更可能影响其他测试，需确认无回归）

- [ ] **Step 7: Commit**

```bash
git add app/gateway.py tests/conftest.py tests/test_gateway_interface.py
git commit -m "feat(gateway): add get_adj_factor to Gateway Protocol + FakeGateway"
```

---

### Task 3: AdjFactorService（新文件）

**Files:**
- Create: `app/adj_factor_service.py`
- Test: `tests/test_adj_factor.py`（本任务先写 service 单元测试，HTTP 测试在 Task 4）

**Interfaces:**
- Consumes: `Gateway.get_adj_factor(codes) -> pd.DataFrame`（Task 2）
- Produces: `AdjFactorService(gateway).query(codes, start_time, end_time) -> list[dict]`，返回 `[{code, trade_date, adj_factor}]`

- [ ] **Step 1: Write failing test** — 创建 `tests/test_adj_factor.py`

```python
from datetime import datetime

import pandas as pd
import pytest

from app.adj_factor_service import AdjFactorService
from app.gateway import GatewayNotReadyError, GatewayQueryError
from tests.conftest import FakeGateway, make_adj_factor_df


def test_query_melts_wide_dataframe():
    """SDK 宽表 → 正确长表 [{code, trade_date, adj_factor}]。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"])
    assert len(data) == 2
    codes = {row["code"] for row in data}
    assert codes == {"000001.SZ", "600000.SH"}
    for row in data:
        assert set(row.keys()) == {"code", "trade_date", "adj_factor"}
        assert row["trade_date"] == "2024-05-30" or row["trade_date"] == "2024-05-31"


def test_query_dropna_filters_non_event_rows():
    """宽表含 NaN 的非除权日行被 dropna 过滤。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"])
    # 宽表 2 日期 × 2 代码 = 4 单元格，2 个 NaN，dropna 后剩 2 行
    assert len(data) == 2


def test_query_date_filter_start_only():
    """start_time 过滤掉早于该日期的行。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"], start_time="2024-05-31")
    assert len(data) == 1
    assert data[0]["trade_date"] == "2024-05-31"
    assert data[0]["code"] == "600000.SH"


def test_query_date_filter_end_only():
    """end_time 过滤掉晚于该日期的行。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"], end_time="2024-05-30")
    assert len(data) == 1
    assert data[0]["trade_date"] == "2024-05-30"
    assert data[0]["code"] == "000001.SZ"


def test_query_date_filter_both_sides():
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ"], start_time="2024-05-30", end_time="2024-05-30")
    assert len(data) == 1
    assert data[0]["code"] == "000001.SZ"


def test_query_empty_dataframe():
    """SDK 返回空 DataFrame → 返回 []。"""
    gw = FakeGateway(ready=True, adj_factor_result=pd.DataFrame())
    svc = AdjFactorService(gw)
    assert svc.query(["000001.SZ"]) == []


def test_query_no_result_returns_empty_list():
    """FakeGateway adj_factor_result=None → 空结果。"""
    gw = FakeGateway(ready=True, adj_factor_result=None)
    svc = AdjFactorService(gw)
    assert svc.query(["000001.SZ"]) == []


def test_query_reversed_dates_raises():
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    with pytest.raises(ValueError, match="start_time must not be later than end_time"):
        svc.query(["000001.SZ"], "2024-05-31", "2024-05-30")


def test_query_invalid_date_format_raises():
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    with pytest.raises(ValueError):
        svc.query(["000001.SZ"], "2024/05/30")


def test_query_iso_datetime_accepted():
    """ISO datetime 应被接受并截断为日期比较。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ"], "2024-05-30T00:00:00", "2024-05-30T23:59:59")
    assert len(data) == 1


def test_query_sdk_not_ready():
    gw = FakeGateway(ready=False)
    svc = AdjFactorService(gw)
    with pytest.raises(GatewayNotReadyError):
        svc.query(["000001.SZ"])


def test_query_optional_dates_none():
    """不传日期 → 不过滤，返回全部事件。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    data = svc.query(["000001.SZ", "600000.SH"])
    assert len(data) == 2


def test_query_passes_codes_to_gateway():
    """codes 原样传给 gateway.get_adj_factor。"""
    gw = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    svc = AdjFactorService(gw)
    svc.query(["000001.SZ", "600000.SH"])
    assert gw.adj_factor_query_calls[0]["codes"] == ["000001.SZ", "600000.SH"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_adj_factor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.adj_factor_service'`

- [ ] **Step 3: Create `app/adj_factor_service.py`**

```python
"""AdjFactorService：HTTP 日期参数 → SDK get_adj_factor → 宽表 melt → 日期过滤 → 序列化。

职责边界：
- 调用 gateway.get_adj_factor 获取 SDK 宽表 DataFrame（index=交易日期, columns=股票代码）
- melt 成长表 [{code, trade_date, adj_factor}]，dropna 过滤非除权日
- 按 start_time/end_time 过滤 trade_date（SDK get_adj_factor 不支持日期参数，服务端过滤）
- 委托 serializer 处理 NumPy/datetime/NaN 类型转换

不负责：字段重命名、单位换算、复权计算（由主项目 YAML field_map 完成）

性能：melt/dropna/过滤在 DataFrame 层向量化完成，避免逐条 dict 操作。
"""

import logging
import time
from datetime import datetime

import pandas as pd

from app.gateway import Gateway
from app.serializer import serialize_dataframe

logger = logging.getLogger("amazingdata.adj_factor")


def _parse_iso(iso: str) -> datetime:
    """ISO 日期/日期时间字符串 → datetime。解析失败抛 ValueError（→ HTTP 422）。

    与 kline_service.to_sdk_date 共享 fromisoformat 解析，错误信息格式一致。
    """
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"date must be YYYY-MM-DD or ISO datetime (e.g. 2024-01-01 / 2024-01-01T00:00:00), got: {iso}"
        )


class AdjFactorService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(
        self,
        codes: list[str],
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """查询除权因子，返回展平后的记录列表 [{code, trade_date, adj_factor}]。

        start_time/end_time 可选；为 None 时该侧不过滤。
        SDK get_adj_factor 不支持日期参数，此处全量拉取后按 trade_date 过滤。
        空结果返回 []（HTTP 层包装为 {"data": []}）。
        """
        start_dt = _parse_iso(start_time) if start_time else None
        end_dt = _parse_iso(end_time) if end_time else None
        if start_dt is not None and end_dt is not None and start_dt > end_dt:
            raise ValueError("start_time must not be later than end_time")

        t0 = time.monotonic()
        df = self._gw.get_adj_factor(codes)
        t1 = time.monotonic()
        records = self._process(df, start_dt, end_dt)
        t2 = time.monotonic()
        logger.info(
            "adj_factor query: gateway=%.3fs process=%.3fs codes=%d records=%d",
            t1 - t0, t2 - t1, len(codes), len(records),
        )
        return records

    @staticmethod
    def _process(df: pd.DataFrame, start_dt, end_dt) -> list[dict]:
        """SDK DataFrame → melt → 日期过滤 → 序列化。"""
        if df is None or df.empty:
            return []
        long_df = AdjFactorService._melt_and_normalize(df)
        long_df = AdjFactorService._filter_by_date(long_df, start_dt, end_dt)
        if long_df.empty:
            return []
        return serialize_dataframe(long_df)

    @staticmethod
    def _melt_and_normalize(df: pd.DataFrame) -> pd.DataFrame:
        """SDK 宽表/长表 → code/trade_date/adj_factor 长表。dropna 过滤非除权日。

        判定顺序：先看是否已是长表（含 code + adj_factor 列），否则按宽表处理。
        """
        if "code" in df.columns and "adj_factor" in df.columns:
            # 长表形态：规范化日期列名
            rename: dict[str, str] = {}
            for src in ("timestamp", "date", "trade_date"):
                if src in df.columns and "trade_date" not in df.columns:
                    rename[src] = "trade_date"
                    break
            if rename:
                df = df.rename(columns=rename)
            df = df.dropna(subset=["adj_factor"])
        else:
            # 宽表形态：index=交易日期, columns=股票代码
            df = df.reset_index()
            date_col = df.columns[0]  # 按位置取日期列（index 可能无名）
            df = df.melt(id_vars=[date_col], var_name="code", value_name="adj_factor")
            df = df.rename(columns={date_col: "trade_date"})
            df = df.dropna(subset=["adj_factor"])
        df = AdjFactorService._normalize_trade_date_str(df)
        return df[["code", "trade_date", "adj_factor"]]

    @staticmethod
    def _normalize_trade_date_str(df: pd.DataFrame) -> pd.DataFrame:
        """trade_date 列统一为 YYYY-MM-DD 字符串。

        datetime64 → dt.strftime；其他类型 → to_datetime 解析后 strftime。
        同 _truncate_kline_time_in_df（kline_service.py:131-141）模式：serialize_dataframe
        会把 datetime 转 ISO datetime，需在此显式截断为日期。
        """
        col = df["trade_date"]
        if pd.api.types.is_datetime64_any_dtype(col):
            df["trade_date"] = col.dt.strftime("%Y-%m-%d")
        else:
            try:
                df["trade_date"] = pd.to_datetime(col, errors="coerce").dt.strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                pass
        return df

    @staticmethod
    def _filter_by_date(df: pd.DataFrame, start_dt, end_dt) -> pd.DataFrame:
        """按 trade_date（YYYY-MM-DD 字符串）过滤。start/end 为 None 时该侧不过滤。"""
        if df.empty:
            return df
        mask = pd.Series([True] * len(df), index=df.index)
        if start_dt is not None:
            mask &= df["trade_date"] >= start_dt.strftime("%Y-%m-%d")
        if end_dt is not None:
            mask &= df["trade_date"] <= end_dt.strftime("%Y-%m-%d")
        return df[mask]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_adj_factor.py -v`
Expected: 全部 PASS（13 个测试）

- [ ] **Step 5: Commit**

```bash
git add app/adj_factor_service.py tests/test_adj_factor.py
git commit -m "feat(service): add AdjFactorService for SDK get_adj_factor melt+filter"
```

---

### Task 4: HTTP 路由（POST /adj_factor）

**Files:**
- Modify: `app/http_app.py`
- Test: `tests/test_adj_factor.py`（追加 HTTP 端到端测试）

**Interfaces:**
- Consumes: `AdjFactorService`（Task 3）、`app.state.sdk_gate`、错误码常量
- Produces: `POST /adj_factor` 端点，请求体 `AdjFactorRequest`，响应 `{"data": [...]}`

- [ ] **Step 1: Write failing test** — 追加到 `tests/test_adj_factor.py`

```python
from app.config import Config
from app.http_app import create_app
from fastapi.testclient import TestClient


def make_test_app(gateway=None):
    config = Config(username="u", password="p", ip="1.2.3.4", port=3021)
    if gateway is None:
        gateway = FakeGateway(ready=True, adj_factor_result=make_adj_factor_df())
    app = create_app(config=config, gateway=gateway)
    return TestClient(app)


def test_adj_factor_http_success():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ", "600000.SH"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert len(body["data"]) == 2
    row = body["data"][0]
    assert set(row.keys()) == {"code", "trade_date", "adj_factor"}


def test_adj_factor_http_field_names_neutral():
    """响应字段为 code/trade_date/adj_factor，非 stocker 的 symbol/ex_factor。"""
    client = make_test_app()
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert "code" in row
    assert "trade_date" in row
    assert "adj_factor" in row
    assert "symbol" not in row
    assert "ex_factor" not in row


def test_adj_factor_http_date_filter():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ", "600000.SH"],
        "start_time": "2024-05-31",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["trade_date"] == "2024-05-31"


def test_adj_factor_http_empty_codes_422():
    client = make_test_app()
    resp = client.post("/adj_factor", json={"codes": []})
    assert resp.status_code == 422


def test_adj_factor_http_reversed_dates_422():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-05-31",
        "end_time": "2024-05-30",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_adj_factor_http_invalid_date_422():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ"],
        "start_time": "2024/05/30",
    })
    assert resp.status_code == 422


def test_adj_factor_http_sdk_not_ready_503():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SDK_NOT_READY"


def test_adj_factor_http_empty_result_200():
    gw = FakeGateway(ready=True, adj_factor_result=None)
    client = make_test_app(gateway=gw)
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_adj_factor_http_optional_dates_missing():
    client = make_test_app()
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_adj_factor_http_iso_datetime_accepted():
    client = make_test_app()
    resp = client.post("/adj_factor", json={
        "codes": ["000001.SZ"],
        "start_time": "2024-05-30T00:00:00",
        "end_time": "2024-05-30T23:59:59",
    })
    assert resp.status_code == 200


def test_adj_factor_http_error_has_request_id():
    gw = FakeGateway(ready=False)
    client = make_test_app(gateway=gw)
    resp = client.post("/adj_factor", json={"codes": ["000001.SZ"]})
    assert "request_id" in resp.json()["error"]
    assert resp.headers.get("X-Request-ID") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_adj_factor.py::test_adj_factor_http_success -v`
Expected: FAIL with 404（路由不存在）

- [ ] **Step 3: Modify `app/http_app.py`** — import + 模型 + 实例化 + 路由

在文件顶部 import 区（`from app.kline_service import KlineService, MINUTE_PERIODS` 之后）新增：

```python
from app.adj_factor_service import AdjFactorService
```

在 `MinuteRequest` 类之后新增 `AdjFactorRequest` 模型：

```python
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
```

在 `create_app` 函数中（`realtime_service = RealtimeService(gateway)` 之后）新增：

```python
    adj_factor_service = AdjFactorService(gateway)
```

在 `app.state.health_service = health_service` 之后新增：

```python
    app.state.adj_factor_service = adj_factor_service
```

在 `@app.post("/minute")` 路由块之后、`@app.get("/realtime")` 之前新增 `/adj_factor` 路由：

```python
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
            logger.error("unhandled error: %s: %s", type(e).__name__, e)
            raise AppError(INTERNAL_ERROR, str(e), 500)
        finally:
            app.state.sdk_gate.release()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_adj_factor.py -v`
Expected: 全部 PASS（service 测试 + HTTP 测试）

- [ ] **Step 5: Run full suite to verify no regression**

Run: `python -m pytest tests/ -v --tb=short`
Expected: 全部 PASS（含原有 /daily /minute /realtime 测试 + 新 /adj_factor 测试）

- [ ] **Step 6: Commit**

```bash
git add app/http_app.py tests/test_adj_factor.py
git commit -m "feat(http): add POST /adj_factor endpoint"
```

---

### Task 5: 全套件验证 + 文档同步

**Files:**
- Verify: 全部测试通过
- Update: `README.md`（如果列了端点）

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: 全部 PASS，无 regression

- [ ] **Step 2: Verify lint / type check（如有）**

Run: `python -c "import app.http_app; import app.adj_factor_service; import app.gateway; print('imports ok')"`
Expected: 输出 `imports ok`（验证无 import 错误）

- [ ] **Step 3: 如 README 列了端点，补充 /adj_factor 说明**

检查 `README.md` 是否列出 `/daily` `/minute` `/realtime`。若有，追加 `/adj_factor` 一行。

- [ ] **Step 4: Final commit（如有文档改动）**

```bash
git add README.md
git commit -m "docs: add /adj_factor endpoint to README"
```

（如 README 无端点列表，跳过此步）

---

## Self-Review

**1. Spec coverage:**
- 接口契约（请求/响应）：Task 4 ✓
- 三层架构：Task 2（gateway）+ Task 3（service）+ Task 4（http）✓
- Config 变更：Task 1 ✓
- FakeGateway + 工厂：Task 2 ✓
- 测试用例（宽表 melt/dropna/日期过滤/空/422/503/502/字段中性/Protocol）：Task 2+3+4 ✓
- 错误码映射：Task 4 路由 handler ✓
- 验证计划（SDK 实测）：留作实现后手动验证（计划外，需真实凭证）

**2. Placeholder scan:** 无 TBD/TODO，所有代码块完整。

**3. Type consistency:**
- `Gateway.get_adj_factor(codes: list[str]) -> pd.DataFrame` — Task 2 定义，Task 3 消费 ✓
- `AdjFactorService(gateway).query(codes, start_time, end_time) -> list[dict]` — Task 3 定义，Task 4 消费 ✓
- `FakeGateway.__init__(ready, result, adj_factor_result)` — Task 2 定义，Task 3+4 测试消费 ✓
- `make_adj_factor_df() -> pd.DataFrame` — Task 2 定义，Task 3+4 测试消费 ✓
- 响应字段 `code`/`trade_date`/`adj_factor` — 全链路一致 ✓

**4. Ambiguity:** 无。melt 判定顺序、日期过滤边界、空值处理均在 Task 3 代码中明确。

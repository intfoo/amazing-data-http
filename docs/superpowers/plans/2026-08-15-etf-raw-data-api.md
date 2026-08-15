# ETF 份额/净值原始数据接口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 `/etf/net_inflow`（服务端宽基筛选+净流入计算），新增 `/etf/share`、`/etf/nav` 两个原始数据接口，宽基识别与净流入计算下移下游。

**Architecture:** 新增薄 Service `FundDataService`（缓存 + 深市日期修正 + 后扩拉取），复用现有 Gateway `get_fund_share`/`get_fund_nav`/`get_code_list` 与 `_run_sdk_endpoint` 错误映射；HTTP 层换路由；旧 `EtfFlowService` 整体删除。

**Tech Stack:** Python 3.13 / FastAPI / pydantic v2 / pandas / pytest（FakeGateway 注入，无需真实 SDK）

**Spec:** `docs/superpowers/specs/2026-08-15-etf-raw-data-api-design.md`

## Global Constraints

- 配置改名：`etf_flow_cache_ttl_sec` → `fund_data_cache_ttl_sec`，env `ETF_FLOW_CACHE_TTL_SEC` → `FUND_DATA_CACHE_TTL_SEC`，默认 300
- `/etf/share` 响应行：`{code, trade_date, share, ann_date}`；`/etf/nav` 响应行：`{code, trade_date, nav}`
- `trade_date` 语义 = 真实交易日：沪市 share 取 `CHANGE_DATE` 原样；深市 `CHANGE_DATE==ANN_DATE` 时用交易日历 snap 前一交易日；nav 取 `PRICE_DATE` 无修正。该语义必须写进代码 docstring 和 docs/API.md
- 两接口拉取区间 `end_date` 均后扩 10 天，拉完按 `trade_date` 过滤回用户区间；**不做前扩**
- 日期双缺省 = 近 30 天；只传一侧则该侧生效；`start>end` → ValueError（HTTP 422）
- codes 缺省 = 全量 ETF，经 `gateway.get_code_list("EXTRA_ETF")`（**不用 get_code_info**，新接口不需要 name）
- 结果缓存 key = `(kind, frozenset(codes) if codes else None, start_time, end_time)`，TTL 300s，64 条上限整体清空；ETF 清单按日缓存 `(date_str, codes)`
- 日期过滤前先 `dropna(subset=["trade_date"])`
- 新请求体**不继承 `_CodesRequest`**（其 validator 强制非空），自定义 `codes: list[str] | None = None`
- 测试命令（Windows，合并执行，禁逐文件）：`.\.venv\Scripts\python.exe -m pytest tests/test_fund_data_service.py tests/test_http_etf.py tests/test_config.py -q > pytest-out.txt 2>&1` 然后读文件；最终全量：`.\.venv\Scripts\python.exe -m pytest -q > pytest-out.txt 2>&1`；`pytest-out.txt` 用完即删
- git 操作用 git MCP，不用终端 git
- 本项目是 pytest（Python），不是 jest；测试用 FakeGateway（tests/conftest.py），无需真实 SDK/网络

---

### Task 1: 配置改名 fund_data_cache_ttl_sec

**Files:**
- Modify: `app/config.py:50`、`:81`
- Modify: `tests/test_config.py:118-122`
- Modify: `.env.example:85` 附近、`:53` 附近注释
- Modify: `README.md:165` 附近配置表

**Interfaces:**
- Produces: `Config.fund_data_cache_ttl_sec: int`（Task 3 的 http_app 装配消费）

- [ ] **Step 1: 改测试先行（失败测试）**

`tests/test_config.py` 中找到 `test_config_etf_flow_cache_ttl`（约 118-122 行，断言 `etf_flow_cache_ttl_sec` 字段与 `ETF_FLOW_CACHE_TTL_SEC` env），整体替换为：

```python
def test_config_fund_data_cache_ttl(monkeypatch):
    """FUND_DATA_CACHE_TTL_SEC 环境变量 → Config.fund_data_cache_ttl_sec。"""
    monkeypatch.delenv("FUND_DATA_CACHE_TTL_SEC", raising=False)
    assert Config.from_env().fund_data_cache_ttl_sec == 300
    monkeypatch.setenv("FUND_DATA_CACHE_TTL_SEC", "600")
    assert Config.from_env().fund_data_cache_ttl_sec == 600
```

（先读该文件确认原测试的确切写法，保持其 monkeypatch 风格；上面是目标形态。）

- [ ] **Step 2: 改 config.py**

`app/config.py:50` 字段行替换为：

```python
    fund_data_cache_ttl_sec: int = 300  # /etf/share、/etf/nav 结果缓存 TTL（秒），份额/净值 T+1 更新无 freshness 风险
```

`:81` from_env 行替换为：

```python
            fund_data_cache_ttl_sec=_env_int("FUND_DATA_CACHE_TTL_SEC", 300),
```

- [ ] **Step 3: 改 .env.example**

`#ETF_FLOW_CACHE_TTL_SEC=300` 行替换为 `#FUND_DATA_CACHE_TTL_SEC=300`，其上方注释中
"/etf/net_inflow 查询结果缓存 TTL" 改为 "/etf/share、/etf/nav 查询结果缓存 TTL"。
另：约 53 行 `FUND_LOCAL_PATH` 注释中 "（/etf/net_inflow 净流入计算依赖）" 改为
"（/etf/share、/etf/nav 依赖）"。

- [ ] **Step 4: 改 README.md**

配置表中含 `ETF_FLOW_CACHE_TTL_SEC` 的行（约 165 行），变量名改 `FUND_DATA_CACHE_TTL_SEC`，
说明文字中 `/etf/net_inflow` 改 `/etf/share、/etf/nav`。

- [ ] **Step 5: 跑测试验证**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q > pytest-out.txt 2>&1
```

读 pytest-out.txt，预期 PASS（除可能存在的与本次无关的既有失败——若有，记录但不处理）。

- [ ] **Step 6: Commit**

git MCP add 上述 4 个文件，commit message: `refactor: etf_flow_cache_ttl_sec 改名 fund_data_cache_ttl_sec`

---

### Task 2: FundDataService 新模块 + 单元测试

**Files:**
- Create: `app/fund_data_service.py`
- Create: `tests/test_fund_data_service.py`
- Test 依赖: `tests/conftest.py` 的 FakeGateway（已有 `get_fund_share`/`get_fund_nav`/`get_code_list`/`calendar`，不改）

**Interfaces:**
- Consumes: `Gateway.get_fund_share(codes, is_local, begin_date, end_date) -> dict[str, DataFrame]`；`Gateway.get_fund_nav(...)` 同签名；`Gateway.get_code_list("EXTRA_ETF") -> list[str]`；`Gateway.calendar -> list[int] | None`；`app.kline_service.to_sdk_date(str) -> int`；`app.serializer.serialize_dataframe(df) -> list[dict]`；`app.subscription_schedule.now_cn() -> datetime`
- Produces: `FundDataService(gateway, cache_ttl_sec=300)`；`.query_share(codes=None, start_time=None, end_time=None) -> list[dict]`；`.query_nav(codes=None, start_time=None, end_time=None) -> list[dict]`（Task 3、Task 4 消费）；模块级 `_to_date_str(s)`（Task 4 探针脚本可能消费）

- [ ] **Step 1: 写测试文件（先失败）**

创建 `tests/test_fund_data_service.py`：

```python
"""FundDataService 单元测试：/etf/share、/etf/nav 原始数据供给。

覆盖：深市 snap 修正（普通日/跨周末/无日历兜底）、沪市原样、后扩 10 天拉取、
按修正后 trade_date 过滤回用户区间、codes 缺省走全量清单、双缺省默认近 30 天、
start>end 422 语义（ValueError）、NaN 日期行丢弃、结果缓存、空结果。
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.fund_data_service import FundDataService
from tests.conftest import FakeGateway


def _share_df(rows: list[tuple[int, float, int]]) -> pd.DataFrame:
    """rows: (CHANGE_DATE, FUND_SHARE, ANN_DATE)。"""
    return pd.DataFrame({
        "CHANGE_DATE": [r[0] for r in rows],
        "FUND_SHARE": [r[1] for r in rows],
        "ANN_DATE": [r[2] for r in rows],
    })


def _nav_df(rows: list[tuple[int, float]]) -> pd.DataFrame:
    """rows: (PRICE_DATE, UNIT_NAV)。"""
    return pd.DataFrame({
        "PRICE_DATE": [r[0] for r in rows],
        "UNIT_NAV": [r[1] for r in rows],
    })


# 2024-01 交易日历片段：01-02(周二)~01-05(周五)、01-08(周一)~01-12(周五)
CAL = [20240102, 20240103, 20240104, 20240105,
       20240108, 20240109, 20240110, 20240111, 20240112]


def test_share_sh_date_passthrough():
    """沪市：CHANGE_DATE(=T) 与 ANN_DATE(=T+1) 不同 → trade_date 取 CHANGE_DATE 原样。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "510300.SH": _share_df([(20240103, 100.0, 20240104),
                                (20240104, 101.0, 20240105)]),
    })
    svc = FundDataService(gw)
    recs = svc.query_share(["510300.SH"], "2024-01-01", "2024-01-31")
    assert [r["trade_date"] for r in recs] == ["2024-01-03", "2024-01-04"]
    assert [r["ann_date"] for r in recs] == ["2024-01-04", "2024-01-05"]
    assert [r["share"] for r in recs] == [100.0, 101.0]
    assert all(r["code"] == "510300.SH" for r in recs)


def test_share_sz_snap_prev_trade_day():
    """深市：CHANGE_DATE==ANN_DATE（公告日 T+1）→ snap 前一交易日还原真实变动日。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "159915.SZ": _share_df([(20240104, 200.0, 20240104)]),  # 公告 01-04 → 真实 01-03
    })
    svc = FundDataService(gw)
    recs = svc.query_share(["159915.SZ"], "2024-01-01", "2024-01-31")
    assert [r["trade_date"] for r in recs] == ["2024-01-03"]
    assert [r["ann_date"] for r in recs] == ["2024-01-04"]


def test_share_sz_snap_across_weekend():
    """深市跨周末：周一公告 → snap 到上周五。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "159915.SZ": _share_df([(20240108, 200.0, 20240108)]),  # 周一公告 → 周五 01-05
    })
    svc = FundDataService(gw)
    recs = svc.query_share(["159915.SZ"], "2024-01-01", "2024-01-31")
    assert [r["trade_date"] for r in recs] == ["2024-01-05"]


def test_share_sz_no_calendar_fallback_minus_1_day():
    """无交易日历：退化为简单减 1 天（可能落周末，仅影响日期落点）。"""
    gw = FakeGateway(ready=True, calendar=None, fund_share_result={
        "159915.SZ": _share_df([(20240108, 200.0, 20240108)]),
    })
    svc = FundDataService(gw)
    recs = svc.query_share(["159915.SZ"], "2024-01-01", "2024-01-31")
    assert [r["trade_date"] for r in recs] == ["2024-01-07"]  # 01-08 减 1 天（周日）


def test_share_fetch_end_extended_10_days():
    """拉取区间 end_date 后扩 10 天（覆盖深市 T+1 公告跨长假）。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "510300.SH": _share_df([(20240104, 100.0, 20240105)]),
    })
    svc = FundDataService(gw)
    svc.query_share(["510300.SH"], "2024-01-01", "2024-01-05")
    call = gw.fund_share_calls[-1]
    assert call["begin_date"] == 20240101
    assert call["end_date"] == 20240115  # 20240105 + 10 天


def test_share_sz_late_ann_included_after_snap_filter():
    """后扩拉到的深市数据 snap 后落在用户区间内 → 正确返回。

    用户 end=01-05（周五），深市 01-08（周一）才公告 01-05 的变动。
    不后扩会拉不到这条；后扩拉到后 snap 回 01-05，过滤后保留。
    """
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "159915.SZ": _share_df([(20240108, 200.0, 20240108)]),
    })
    svc = FundDataService(gw)
    recs = svc.query_share(["159915.SZ"], "2024-01-01", "2024-01-05")
    assert [r["trade_date"] for r in recs] == ["2024-01-05"]


def test_share_out_of_range_filtered_after_snap():
    """snap 后落在用户区间外 → 被过滤。用户 start=01-08，01-08 公告 snap 回 01-05 < start。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "159915.SZ": _share_df([(20240108, 200.0, 20240108)]),
    })
    svc = FundDataService(gw)
    recs = svc.query_share(["159915.SZ"], "2024-01-08", "2024-01-31")
    assert recs == []


def test_nav_uses_price_date_no_snap():
    """净值：trade_date=PRICE_DATE 原样（深市也无需修正），响应无 ann_date。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_nav_result={
        "159915.SZ": _nav_df([(20240104, 1.234), (20240105, 1.245)]),
    })
    svc = FundDataService(gw)
    recs = svc.query_nav(["159915.SZ"], "2024-01-01", "2024-01-31")
    assert [r["trade_date"] for r in recs] == ["2024-01-04", "2024-01-05"]
    assert [r["nav"] for r in recs] == [1.234, 1.245]
    assert set(recs[0].keys()) == {"code", "trade_date", "nav"}


def test_nav_fetch_end_extended_10_days():
    """净值同样后扩 10 天（T+1 入库）。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_nav_result={
        "510300.SH": _nav_df([(20240104, 4.1)]),
    })
    svc = FundDataService(gw)
    svc.query_nav(["510300.SH"], "2024-01-01", "2024-01-05")
    assert gw.fund_nav_calls[-1]["end_date"] == 20240115


def test_codes_default_uses_full_etf_list():
    """codes 缺省 → get_code_list('EXTRA_ETF') 全量清单（按日缓存，二次调用不重取）。"""
    gw = FakeGateway(ready=True, calendar=CAL,
                     etf_code_list=["510300.SH", "159915.SZ"],
                     fund_share_result={
                         "510300.SH": _share_df([(20240104, 1.0, 20240105)]),
                         "159915.SZ": _share_df([(20240104, 2.0, 20240104)]),
                     })
    svc = FundDataService(gw)
    recs = svc.query_share(None, "2024-01-01", "2024-01-31")
    assert {r["code"] for r in recs} == {"510300.SH", "159915.SZ"}
    assert gw.fund_share_calls[-1]["codes"] == ["510300.SH", "159915.SZ"]


def test_default_range_last_30_days():
    """日期双缺省 → 近 30 天（用 now_cn 当日为终点）。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={})
    svc = FundDataService(gw)
    svc.query_share(["510300.SH"])
    call = gw.fund_share_calls[-1]
    assert call["begin_date"] is not None and call["end_date"] is not None
    # begin 约为 30 天前（容差 2 天防跨月边界）
    from app.subscription_schedule import now_cn
    from datetime import timedelta
    expect_begin = int((now_cn() - timedelta(days=30)).strftime("%Y%m%d"))
    assert abs(call["begin_date"] - expect_begin) <= 2


def test_start_after_end_raises():
    gw = FakeGateway(ready=True, calendar=CAL)
    svc = FundDataService(gw)
    with pytest.raises(ValueError, match="start_time"):
        svc.query_share(["510300.SH"], "2024-02-01", "2024-01-01")
    with pytest.raises(ValueError, match="start_time"):
        svc.query_nav(["510300.SH"], "2024-02-01", "2024-01-01")


def test_nan_change_date_row_dropped():
    """CHANGE_DATE 为 NaN 的行被丢弃，不进入响应也不炸过滤。"""
    df = _share_df([(20240104, 100.0, 20240105)])
    df.loc[1] = [None, 50.0, 20240106]
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={"510300.SH": df})
    svc = FundDataService(gw)
    recs = svc.query_share(["510300.SH"], "2024-01-01", "2024-01-31")
    assert len(recs) == 1
    assert recs[0]["trade_date"] == "2024-01-04"


def test_result_cache_hit():
    """相同 (codes, start, end) 300s 内命中缓存，不重复调 SDK；codes 顺序不同也命中（frozenset）。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={
        "510300.SH": _share_df([(20240104, 100.0, 20240105)]),
        "159915.SZ": _share_df([(20240104, 200.0, 20240104)]),
    })
    svc = FundDataService(gw)
    svc.query_share(["510300.SH", "159915.SZ"], "2024-01-01", "2024-01-31")
    svc.query_share(["159915.SZ", "510300.SH"], "2024-01-01", "2024-01-31")
    assert len(gw.fund_share_calls) == 1


def test_empty_result():
    """SDK 返回空 dict / 区间内无数据 → []。"""
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={}, fund_nav_result={})
    svc = FundDataService(gw)
    assert svc.query_share(["510300.SH"], "2024-01-01", "2024-01-31") == []
    assert svc.query_nav(["510300.SH"], "2024-01-01", "2024-01-31") == []


def test_share_and_nav_cache_independent():
    """share 与 nav 缓存 key 含 kind，互不串。"""
    gw = FakeGateway(ready=True, calendar=CAL,
                     fund_share_result={"510300.SH": _share_df([(20240104, 1.0, 20240105)])},
                     fund_nav_result={"510300.SH": _nav_df([(20240104, 4.1)])})
    svc = FundDataService(gw)
    svc.query_share(["510300.SH"], "2024-01-01", "2024-01-31")
    svc.query_nav(["510300.SH"], "2024-01-01", "2024-01-31")
    assert len(gw.fund_share_calls) == 1 and len(gw.fund_nav_calls) == 1
```

- [ ] **Step 2: 跑测试确认全部失败（模块不存在）**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_fund_data_service.py -q > pytest-out.txt 2>&1
```

读 pytest-out.txt，预期 collection error / ModuleNotFoundError: app.fund_data_service。

- [ ] **Step 3: 实现 app/fund_data_service.py**

完整文件内容：

```python
"""FundDataService：ETF 份额/净值原始数据 Service 层（/etf/share、/etf/nav）。

职责边界：
- codes 缺省时经 gateway.get_code_list("EXTRA_ETF") 取全量 ETF（按日缓存）
- 深市份额 CHANGE_DATE 修正（snap 前一交易日）——数据准确性修复，非业务计算
- 拉取区间 end_date 后扩 10 天（覆盖深市 T+1 公告 / 净值 T+1 入库跨长假）
- 日期过滤 + 序列化

不负责（下游职责）：宽基 ETF 识别、净流入计算（share.diff() × nav）、share/nav join。

trade_date 语义（两接口统一 = 真实交易日，下游按 (code, trade_date) 直接 join）：
- share：沪市取 SDK CHANGE_DATE 原样（=T，可信）；深市 SDK CHANGE_DATE 实际填公告日
  T+1（与 ANN_DATE 相同），服务端用交易日历 snap 前一交易日还原为 T
- nav：取 SDK PRICE_DATE（净值计算日，沪深均准确，无需修正）

日期语义背景（深市份额）：
ETF 份额变动是 T 日收盘后的申赎结果，深市 T+1 才公告。若 T 为周五/节前最后一天，
公告落在下周一/节后（最长约 10 天），故拉取区间后扩 10 天再在 trade_date 上过滤。

详见 docs/API.md 的 /etf/share、/etf/nav 章节。
"""

from __future__ import annotations

import bisect
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.gateway import Gateway
from app.kline_service import to_sdk_date
from app.serializer import serialize_dataframe
from app.subscription_schedule import now_cn

logger = logging.getLogger("amazingdata.fund_data")

_DEFAULT_RANGE_DAYS = 30     # start_time/end_time 双缺省时的默认区间
_FETCH_END_EXTEND_DAYS = 10  # 深市 T+1 公告 / 净值 T+1 入库跨长假的最长覆盖
_RESULT_CACHE_MAX = 64       # 结果缓存上限，超限整体清空


def _to_date_str(s) -> "pd.Series":
    """将日期 Series 统一转为 YYYY-MM-DD 字符串。

    处理 datetime64/int/string 三种类型。int 格式（如 20240102）需用 format="%Y%m%d"
    解析，否则 pd.to_datetime 会将其视为纳秒时间戳 → 1970 年。NaT/NaN → NaN。
    """
    import pandas as pd
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d")
    elif pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        return pd.to_datetime(s, errors="coerce").dt.strftime("%Y-%m-%d")


class FundDataService:
    """ETF 份额/净值原始数据 Service。

    缓存语义：
    - 查询结果按 (kind, frozenset(codes)|None, start_time, end_time) 缓存，
      TTL 由 cache_ttl_sec 控制（默认 300s）。份额/净值 T+1 更新，300s 无 freshness 风险。
      上限 64 条，超限整体清空。
    - 全量 ETF 清单按日缓存 (date_str, codes)，跨日自动失效重取。
    """

    def __init__(self, gateway: Gateway, cache_ttl_sec: int = 300):
        self._gw = gateway
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_lock = threading.Lock()
        self._list_cache: tuple[str, list[str]] | None = None
        self._result_cache: dict[tuple, tuple[float, list[dict]]] = {}

    def query_share(
        self,
        codes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """份额原始时序。返回 [{code, trade_date, share, ann_date}]，语义见模块 docstring。"""
        return self._query("share", codes, start_time, end_time)

    def query_nav(
        self,
        codes: list[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        """净值原始时序。返回 [{code, trade_date, nav}]，trade_date=PRICE_DATE 无需修正。"""
        return self._query("nav", codes, start_time, end_time)

    def _query(
        self,
        kind: str,
        codes: list[str] | None,
        start_time: str | None,
        end_time: str | None,
    ) -> list[dict]:
        # 1. 日期解析：双缺省默认近 30 天；start<=end 校验（ValueError → HTTP 422）
        if start_time is None and end_time is None:
            now_ts = now_cn()
            start_time = (now_ts - timedelta(days=_DEFAULT_RANGE_DAYS)).strftime("%Y-%m-%d")
            end_time = now_ts.strftime("%Y-%m-%d")
            logger.debug("fund_data 默认范围: %s..%s (近 %d 天)",
                         start_time, end_time, _DEFAULT_RANGE_DAYS)
        begin_date = to_sdk_date(start_time) if start_time else None
        end_date = to_sdk_date(end_time) if end_time else None
        if begin_date is not None and end_date is not None and begin_date > end_date:
            raise ValueError("start_time must not be later than end_time")

        # 2. 结果缓存：frozenset 消除 codes 顺序敏感；缺省用 None 哨兵
        cache_key = (kind, frozenset(codes) if codes else None, start_time, end_time)
        now_mono = time.monotonic()
        with self._cache_lock:
            hit = self._result_cache.get(cache_key)
            if hit is not None and now_mono - hit[0] <= self._cache_ttl_sec:
                logger.info("fund_data 结果缓存命中: kind=%s key=%s records=%d",
                            kind, cache_key[1:], len(hit[1]))
                return list(hit[1])

        # 3. codes 解析：缺省走全量 ETF 清单（按日缓存）
        resolved = list(codes) if codes else self._get_all_etf_codes()
        if not resolved:
            return []

        # 4. 拉取区间 end 后扩 10 天（深市 T+1 公告 / 净值 T+1 入库跨长假），
        #    拉完按 trade_date 过滤回用户区间。不做前扩（diff 是下游职责）。
        fetch_end = end_date
        if end_date is not None:
            fetch_end = int(
                (datetime.strptime(str(end_date), "%Y%m%d")
                 + timedelta(days=_FETCH_END_EXTEND_DAYS)).strftime("%Y%m%d")
            )

        # 5. 拉取（is_local/local_path 由 Gateway 内部从 Config 读取）
        t0 = time.monotonic()
        if kind == "share":
            data = self._gw.get_fund_share(
                resolved, is_local=False, begin_date=begin_date, end_date=fetch_end)
        else:
            data = self._gw.get_fund_nav(
                resolved, is_local=False, begin_date=begin_date, end_date=fetch_end)
        logger.info("fund_data fetch: kind=%s %.3fs codes=%d",
                    kind, time.monotonic() - t0, len(resolved))

        # 6. 规范化 + 过滤 + 序列化
        records = self._build_records(
            kind, resolved, data, start_time, end_time, self._gw.calendar)

        with self._cache_lock:
            if len(self._result_cache) >= _RESULT_CACHE_MAX:
                self._result_cache.clear()
            self._result_cache[cache_key] = (time.monotonic(), records)
        return records

    def _get_all_etf_codes(self) -> list[str]:
        """全量 ETF 代码清单，按日缓存（key=当日日期字符串）。"""
        today = now_cn().strftime("%Y-%m-%d")
        with self._cache_lock:
            if self._list_cache and self._list_cache[0] == today:
                return list(self._list_cache[1])
        codes = list(self._gw.get_code_list(security_type="EXTRA_ETF") or [])
        with self._cache_lock:
            self._list_cache = (today, codes)
        logger.info("fund_data 全量 ETF 清单: %d 只", len(codes))
        return list(codes)

    @staticmethod
    def _build_records(
        kind: str,
        codes: list[str],
        data_dict: dict[str, "pd.DataFrame"],
        start_str: str | None,
        end_str: str | None,
        calendar: list[int] | None,
    ) -> list[dict]:
        """规范化每只 ETF 的 DataFrame → 过滤用户区间 → 合并序列化。

        日期过滤统一用 YYYY-MM-DD 字符串比较；过滤前 dropna 丢弃日期缺失行。
        """
        import pandas as pd

        frames: list[pd.DataFrame] = []
        for code in codes:
            df = data_dict.get(code)
            if df is None or df.empty:
                continue
            if kind == "share":
                norm = FundDataService._normalize_share_df(df, code, calendar)
            else:
                norm = FundDataService._normalize_nav_df(df)
            # SDK 返回 CHANGE_DATE/PRICE_DATE 为 NaN 的行：丢弃，不参与日期比较
            norm = norm.dropna(subset=["trade_date"])
            if start_str is not None:
                norm = norm[norm["trade_date"] >= start_str]
            if end_str is not None:
                norm = norm[norm["trade_date"] <= end_str]
            if norm.empty:
                continue
            norm["code"] = code
            frames.append(norm)

        if not frames:
            return []
        combined = pd.concat(frames, ignore_index=True)
        cols = (["code", "trade_date", "share", "ann_date"] if kind == "share"
                else ["code", "trade_date", "nav"])
        return serialize_dataframe(combined[cols])

    @staticmethod
    def _normalize_share_df(
        df: "pd.DataFrame",
        code: str = "",
        calendar: list[int] | None = None,
    ) -> "pd.DataFrame":
        """规范化份额 DataFrame → trade_date/share/ann_date 三列，按 trade_date 排序。

        SDK 返回含 FUND_SHARE/CHANGE_DATE/ANN_DATE。
        - trade_date = 份额实际变动交易日 T；ann_date = 原始公告日（供审计）
        - 沪市：CHANGE_DATE=T（可信），ANN_DATE=T+1
        - 深市：CHANGE_DATE==ANN_DATE（都填公告日 T+1，CHANGE_DATE 不可信），
          用交易日历 snap 到小于 CHANGE_DATE 的最大交易日还原 T；
          无日历时退化为简单减 1 天（可能落周末，仅影响日期落点，记 warning）
        """
        import pandas as pd

        if "CHANGE_DATE" in df.columns:
            date_series = df["CHANGE_DATE"]
        elif "ANN_DATE" in df.columns:
            date_series = df["ANN_DATE"]
        else:
            date_series = df.index

        dates = _to_date_str(date_series)

        is_sz = code.endswith(".SZ")
        if is_sz and "CHANGE_DATE" in df.columns and "ANN_DATE" in df.columns:
            same_as_ann = (df["CHANGE_DATE"] == df["ANN_DATE"]).all()
            if same_as_ann:
                dt = pd.to_datetime(dates, format="%Y-%m-%d", errors="coerce")
                if calendar:
                    cal_sorted = sorted(set(calendar))

                    def snap_to_prev_trade(d):
                        if pd.isna(d):
                            return d
                        d_int = int(d.strftime("%Y%m%d"))
                        idx = bisect.bisect_left(cal_sorted, d_int)
                        if idx > 0:
                            return pd.to_datetime(str(cal_sorted[idx - 1]), format="%Y%m%d")
                        return d  # 日历里找不到，保持原值

                    dt = dt.apply(snap_to_prev_trade)
                else:
                    logger.warning(
                        "fund_data 深市份额修正无交易日历，退化为减 1 天: code=%s", code)
                    dt = dt - pd.Timedelta(days=1)
                dates = dt.dt.strftime("%Y-%m-%d")

        if "ANN_DATE" in df.columns:
            ann_dates = _to_date_str(df["ANN_DATE"])
        else:
            ann_dates = pd.Series([None] * len(df))

        result = pd.DataFrame({
            "trade_date": dates.values if hasattr(dates, "values") else dates,
            "share": df["FUND_SHARE"].values if "FUND_SHARE" in df.columns else None,
            "ann_date": ann_dates.values if hasattr(ann_dates, "values") else ann_dates,
        })
        return result.sort_values("trade_date").reset_index(drop=True)

    @staticmethod
    def _normalize_nav_df(df: "pd.DataFrame") -> "pd.DataFrame":
        """规范化净值 DataFrame → trade_date/nav 两列，按 trade_date 排序。

        SDK 返回含 UNIT_NAV/PRICE_DATE/ANN_DATE。
        trade_date = PRICE_DATE（净值计算日 T，沪深均准确，无需修正、无需 snap）。
        """
        import pandas as pd

        if "PRICE_DATE" in df.columns:
            date_series = df["PRICE_DATE"]
        elif "ANN_DATE" in df.columns:
            date_series = df["ANN_DATE"]
        else:
            date_series = df.index

        dates = _to_date_str(date_series)

        result = pd.DataFrame({
            "trade_date": dates.values if hasattr(dates, "values") else dates,
            "nav": df["UNIT_NAV"].values if "UNIT_NAV" in df.columns else None,
        })
        return result.sort_values("trade_date").reset_index(drop=True)
```

- [ ] **Step 4: 跑测试验证全 PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_fund_data_service.py -q > pytest-out.txt 2>&1
```

读 pytest-out.txt，预期 15 passed。若有失败，从完整日志定位修复，不逐用例重跑。

- [ ] **Step 5: Commit**

git MCP add `app/fund_data_service.py`、`tests/test_fund_data_service.py`，
commit message: `feat: FundDataService ETF 份额/净值原始数据 Service`

---

### Task 3: HTTP 层接线 + 路由测试改写

**Files:**
- Modify: `app/http_app.py:34`（import）、`:131-134`（请求体）、`:207`（装配）、`:343-355`（路由）
- Rewrite: `tests/test_http_etf.py`

**Interfaces:**
- Consumes: `FundDataService.query_share/query_nav`（Task 2）、`Config.fund_data_cache_ttl_sec`（Task 1）
- Produces: `POST /etf/share`、`POST /etf/nav`，请求体 `EtfFundDataRequest{codes: list[str] | None, start_time: str | None, end_time: str | None}`

- [ ] **Step 1: 改写 tests/test_http_etf.py（先失败）**

先读现有文件与 `tests/test_http_*.py` 中 `make_test_app` 的用法保持一致。整体替换为：

```python
"""POST /etf/share、/etf/nav 路由级测试。"""

from __future__ import annotations

import pandas as pd

from tests.conftest import FakeGateway
# make_test_app 的 import 路径以现有 test_http_etf.py 为准（读文件确认后沿用）


def _share_df():
    return pd.DataFrame({
        "CHANGE_DATE": [20240103, 20240104],
        "FUND_SHARE": [100.0, 101.0],
        "ANN_DATE": [20240104, 20240105],
    })


def _nav_df():
    return pd.DataFrame({
        "PRICE_DATE": [20240103, 20240104],
        "UNIT_NAV": [4.1, 4.2],
    })


CAL = [20240102, 20240103, 20240104, 20240105, 20240108]


def test_etf_share_basic():
    gw = FakeGateway(ready=True, calendar=CAL,
                     fund_share_result={"510300.SH": _share_df()})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/share", json={
        "codes": ["510300.SH"], "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 2
    assert set(rows[0].keys()) == {"code", "trade_date", "share", "ann_date"}
    assert rows[0]["trade_date"] == "2024-01-03"


def test_etf_share_codes_optional():
    """codes 缺省 → 全量 ETF 清单，200。"""
    gw = FakeGateway(ready=True, calendar=CAL,
                     etf_code_list=["510300.SH"],
                     fund_share_result={"510300.SH": _share_df()})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/share", json={
        "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert [r["code"] for r in resp.json()["data"]] == ["510300.SH"] * 2


def test_etf_nav_basic():
    gw = FakeGateway(ready=True, calendar=CAL,
                     fund_nav_result={"510300.SH": _nav_df()})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/nav", json={
        "codes": ["510300.SH"], "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 2
    assert set(rows[0].keys()) == {"code", "trade_date", "nav"}
    assert rows[1]["nav"] == 4.2


def test_etf_share_start_after_end_422():
    gw = FakeGateway(ready=True, calendar=CAL, fund_share_result={})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/share", json={
        "codes": ["510300.SH"], "start_time": "2024-02-01", "end_time": "2024-01-01",
    })
    assert resp.status_code == 422


def test_etf_nav_empty_result():
    gw = FakeGateway(ready=True, calendar=CAL, fund_nav_result={})
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/nav", json={
        "codes": ["510300.SH"], "start_time": "2024-01-01", "end_time": "2024-01-31",
    })
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_etf_net_inflow_removed():
    """旧接口 /etf/net_inflow 已下线：404 或 405。"""
    gw = FakeGateway(ready=True)
    client = make_test_app(gateway=gw)
    resp = client.post("/etf/net_inflow", json={})
    assert resp.status_code in (404, 405)
```

（`make_test_app` 的确切来源以读到的旧 test_http_etf.py import 为准；若旧文件里定义在本文件内，则保留该 helper 定义。）

- [ ] **Step 2: 跑测试确认失败**（路由 404/装配缺失）

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_http_etf.py -q > pytest-out.txt 2>&1
```

- [ ] **Step 3: 改 app/http_app.py**

3a. `:34` import 行替换：

```python
from app.fund_data_service import FundDataService
```

3b. `:131-134` `EtfNetInflowRequest` 类替换为：

```python
class EtfFundDataRequest(BaseModel):
    """POST /etf/share、/etf/nav 请求体。codes 可缺省（=全量 ETF），日期可选。

    不继承 _CodesRequest（其 validator 强制 codes 非空）。
    """
    codes: list[str] | None = None
    start_time: str | None = None
    end_time: str | None = None
```

3c. `:207` 装配行替换：

```python
    fund_data_service = FundDataService(gateway, cache_ttl_sec=config.fund_data_cache_ttl_sec)
```

`app.state.etf_flow_service` 赋值处（约 :291）改为 `app.state.fund_data_service = fund_data_service`。

3d. `:343-355` `/etf/net_inflow` 路由整体替换为：

```python
    @app.post("/etf/share")
    async def etf_share(req: EtfFundDataRequest, request: Request):
        """ETF 份额原始时序。返回 {"data": [{code, trade_date, share, ann_date}]}。

        trade_date=份额实际变动交易日（深市 CHANGE_DATE 为公告日 T+1，服务端已 snap 还原，
        详见 docs/API.md）。宽基识别与净流入计算由下游负责。
        """
        logger.info("request_id=%s /etf/share codes=%s %s..%s",
                    get_request_id(request),
                    len(req.codes) if req.codes else "(all)",
                    req.start_time or "(default)", req.end_time or "(default)")
        return await _run_sdk_endpoint(
            app, app.state.fund_data_service.query_share,
            req.codes, req.start_time, req.end_time
        )

    @app.post("/etf/nav")
    async def etf_nav(req: EtfFundDataRequest, request: Request):
        """ETF 净值原始时序。返回 {"data": [{code, trade_date, nav}]}。

        trade_date=PRICE_DATE（净值计算日，沪深均准确）。
        """
        logger.info("request_id=%s /etf/nav codes=%s %s..%s",
                    get_request_id(request),
                    len(req.codes) if req.codes else "(all)",
                    req.start_time or "(default)", req.end_time or "(default)")
        return await _run_sdk_endpoint(
            app, app.state.fund_data_service.query_nav,
            req.codes, req.start_time, req.end_time
        )
```

- [ ] **Step 4: 跑测试验证 PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_http_etf.py tests/test_fund_data_service.py -q > pytest-out.txt 2>&1
```

预期全部 PASS。

- [ ] **Step 5: Commit**

git MCP add `app/http_app.py`、`tests/test_http_etf.py`，
commit message: `feat: /etf/share、/etf/nav 路由替代 /etf/net_inflow`

---

### Task 4: 删除旧 Service 与探针脚本处理

**Files:**
- Delete: `app/etf_flow_service.py`
- Delete: `tests/test_etf_flow_service.py`
- Delete: `scripts/probe_etf_list_all.py`（宽基筛选标注脚本，逻辑下移后失去存在基础）
- Modify: `scripts/probe_etf_single.py:37`、`:70` 附近
- Modify: `scripts/probe_etf_dynamic.py:52-54`、`:111` 附近

**Interfaces:**
- Consumes: `app.fund_data_service.FundDataService._normalize_share_df/_normalize_nav_df`（Task 2）

- [ ] **Step 1: 改 probe_etf_single.py**

先读文件确认现状。`:37` import 改为：

```python
from app.fund_data_service import FundDataService
```

`:70` 附近 `EtfFlowService._compute_net_inflow(codes, name_map, share_dict, nav_dict, start_dt, end_dt, calendar)` 调用改为脚本内自行计算（探针即下游最小样本）：

```python
records = []
for code in codes:
    share_df = FundDataService._normalize_share_df(share_dict.get(code), code, calendar)
    nav_df = FundDataService._normalize_nav_df(nav_dict.get(code))
    merged = share_df.merge(nav_df, on="trade_date", how="left")
    merged["nav"] = merged["nav"].ffill()
    merged["net_inflow_share"] = merged["share"].diff()
    merged["net_inflow_amount"] = merged["net_inflow_share"] * merged["nav"]
    merged = merged[(merged["trade_date"] >= start_dt.strftime("%Y-%m-%d"))
                    & (merged["trade_date"] <= end_dt.strftime("%Y-%m-%d"))]
    for _, row in merged.iterrows():
        records.append({"code": code, "name": name_map.get(code, ""), **row.to_dict()})
```

（读文件后按实际变量名/后续打印逻辑适配，保持脚本原有输出格式。）

- [ ] **Step 2: 改 probe_etf_dynamic.py**

读文件确认现状。该脚本引用了 `BROAD_BASED_KEYWORDS`/`EXCLUDE_KEYWORDS`/`EtfFlowService._filter_broad_based`（已删）。处理：宽基筛选在脚本内嵌一份精简关键词表（从 git 历史复制原常量进脚本，探针自包含），`_filter_broad_based` 调用替换为脚本内本地函数（同样从原实现复制），`_compute_net_inflow` 调用按 Step 1 同款方式替换。原则：探针保持可用、自包含，不 import 任何已删除符号。

- [ ] **Step 3: 删除 3 个文件**

delete_files：`app/etf_flow_service.py`、`tests/test_etf_flow_service.py`、`scripts/probe_etf_list_all.py`。

- [ ] **Step 4: 全仓验证无残留引用**

codespelunker.search(query="etf_flow_service OR EtfFlowService OR net_inflow", path 全仓, snippet_mode="grep", context=2)：
预期命中仅存在于 docs/superpowers/（历史文档）与 .superpowers/（工作记录），
`app/`、`tests/`、`scripts/` 下零命中（API.md 在 Task 5 处理，若本轮命中 docs/API.md 属预期）。

- [ ] **Step 5: 语法检查 + 受影响测试**

```powershell
.\.venv\Scripts\python.exe -m py_compile app\http_app.py app\fund_data_service.py scripts\probe_etf_single.py scripts\probe_etf_dynamic.py && .\.venv\Scripts\python.exe -m pytest tests/test_fund_data_service.py tests/test_http_etf.py tests/test_config.py -q > pytest-out.txt 2>&1
```

预期编译通过、测试全 PASS。

- [ ] **Step 6: Commit**

git MCP add 全部变更，commit message: `refactor: 删除 EtfFlowService 与宽基筛选逻辑，探针脚本自包含化`

---

### Task 5: API 文档更新 + 全量回归

**Files:**
- Modify: `docs/API.md:127-176`（/etf/net_inflow 章节）、`:281`（认证章节）

**Interfaces:**
- Consumes: Task 1-4 全部完成的最终行为

- [ ] **Step 1: 替换 docs/API.md 的 /etf/net_inflow 章节**

先读 `docs/API.md:120-180` 确认现有结构，将该章节整体替换为（标题层级/风格与现有章节对齐）：

```markdown
## POST /etf/share

查询 ETF 份额原始时序。

**职责边界**：本接口只提供原始数据。宽基 ETF 识别、净流入计算
（`share.diff() × nav`，按 `(code, trade_date)` 与 /etf/nav join）由调用方负责。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| codes | string[] | 否 | ETF 代码；缺省 = 全量 ETF |
| start_time | string | 否 | 开始日期 YYYY-MM-DD；与 end_time 双缺省 = 近 30 天 |
| end_time | string | 否 | 结束日期，同上 |

注意：只传 end_time 时从最早可用数据（约 2012 年）开始拉取，
全量 ETF × 全历史数据量很大，请谨慎使用。

**响应**：`{"data": [{code, trade_date, share, ann_date}]}`

| 字段 | 说明 |
|------|------|
| trade_date | 份额**实际变动交易日** T。沪市 = 原始变动日；深市原始数据的变动日字段填的是公告日 T+1，服务端已用交易日历还原为 T |
| share | 基金份额（万份） |
| ann_date | 原始公告日（T+1），供审计追溯 |

**数据延迟**：份额为 T 日收盘后申赎结果，T+1 才公告。查询含最近 1~2 个交易日时，
深市最新一天数据可能缺失（节后公告最长滞后约 10 天，服务端已自动覆盖）。

## POST /etf/nav

查询 ETF 净值原始时序。请求体同 /etf/share。

**响应**：`{"data": [{code, trade_date, nav}]}`

| 字段 | 说明 |
|------|------|
| trade_date | 净值计算日 T（沪深均准确，无修正） |
| nav | 单位净值 |

**数据延迟**：净值 T+1 入库，当日净值次日可查。
```

- [ ] **Step 2: 改认证章节接口清单**

`docs/API.md` 约 281 行，列举需要 Bearer Token 的接口：`/etf/net_inflow` 改为
`/etf/share`、`/etf/nav`。

- [ ] **Step 3: 全量回归**

```powershell
.\.venv\Scripts\python.exe -m pytest -q > pytest-out.txt 2>&1
```

读 pytest-out.txt 找 `passed`/`failed` 汇总行。预期全量通过（基线 420 passed 左右，
减去删除的 test_etf_flow_service.py 用例数，加上新增的 test_fund_data_service.py 15 个
与 test_http_etf.py 6 个）。若有失败从完整日志定位修复。删 pytest-out.txt。

- [ ] **Step 4: Commit**

git MCP add `docs/API.md`（及若有修复的代码），
commit message: `docs: API.md 更新 /etf/share、/etf/nav 章节`

---

## Self-Review 记录

- Spec 覆盖：决策 1（两端点）→ Task 2/3；决策 2（codes 缺省全量）→ Task 2 `_get_all_etf_codes`；决策 3（trade_date 修正+ann_date+文档标注）→ Task 2 `_normalize_share_df` + Task 5；决策 4（删旧接口）→ Task 3/4；决策 5（默认 30 天）→ Task 2 `_query`。评审修订项：ann_date 改造（Task 2 实现含 ann_date 列）、probe_etf_list_all 删除（Task 4）、test_config（Task 1）、frozenset 缓存 key（Task 2）、dropna（Task 2）、nav 后扩（Task 2 `_query` 统一）、只传 end_time 风险标注（Task 5 文档）、_CodesRequest 不复用（Task 3 Step 3b）、.env.example FUND_LOCAL_PATH 注释（Task 1 Step 3）、API.md 认证章节（Task 5 Step 2）✅
- 类型一致性：`query_share(codes, start_time, end_time)` 位置参数在 Task 3 的 `_run_sdk_endpoint(app, fn, req.codes, req.start_time, req.end_time)` 调用中顺序一致 ✅；`Config.fund_data_cache_ttl_sec` 在 Task 1 产出、Task 3 消费 ✅
- 占位符扫描：Task 4 Step 1/2 需先读探针文件适配（已注明），其余步骤代码完整

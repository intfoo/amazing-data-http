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

"""EtfFlowService 单元测试：宽基识别 + 净流入计算 + 端到端 query。"""
from datetime import datetime

import pandas as pd
import pytest

from app.etf_flow_service import EtfFlowService
from app.gateway import GatewayNotReadyError
from tests.conftest import FakeGateway


# ========== 辅助函数：构造 mock 数据 ==========

def make_code_info_df() -> pd.DataFrame:
    """模拟 SDK get_code_info('EXTRA_ETF') 返回的 DataFrame。

    index=ETF 代码，含 symbol 列（证券简称）。
    """
    data = {
        "symbol": [
            "沪深300ETF",       # 宽基 → 纳入
            "沪深300增强ETF",   # 含"增强" → 排除
            "医药ETF",          # 含"医药" → 排除
            "",                 # 空简称 → 跳过
            "中证500ETF",       # 宽基 → 纳入
            "创业板指ETF",      # 宽基 → 纳入
        ],
    }
    return pd.DataFrame(
        data,
        index=pd.Index(
            ["510300.SH", "510301.SH", "159999.SZ", "510302.SH", "510500.SH", "159915.SZ"],
            name="code",
        ),
    )


def make_share_df(code: str = "510300.SH") -> pd.DataFrame:
    """模拟 SDK get_fund_share 返回的 DataFrame。

    index=日期，含 FUND_SHARE/CHANGE_DATE/ANN_DATE 列。
    3 天数据：100, 100, 120 万份（第 2 天不变，第 3 天增加 20 万份）。
    ANN_DATE 与 CHANGE_DATE 相同（简化 mock）。
    """
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    return pd.DataFrame(
        {
            "FUND_SHARE": [100.0, 100.0, 120.0],
            "CHANGE_DATE": [20240102, 20240103, 20240104],
            "ANN_DATE": [20240102, 20240103, 20240104],
        },
        index=pd.Index(dates, name="date"),
    )


def make_nav_df(code: str = "510300.SH") -> pd.DataFrame:
    """模拟 SDK get_fund_nav 返回的 DataFrame。

    index=日期，含 UNIT_NAV/PRICE_DATE/ANN_DATE 列。
    3 天数据：净值 1.0, 1.0, 1.05。
    ANN_DATE 与 PRICE_DATE 相同（简化 mock）。
    """
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    return pd.DataFrame(
        {
            "UNIT_NAV": [1.0, 1.0, 1.05],
            "PRICE_DATE": [20240102, 20240103, 20240104],
            "ANN_DATE": [20240102, 20240103, 20240104],
        },
        index=pd.Index(dates, name="date"),
    )


def make_nav_df_missing(code: str = "510300.SH") -> pd.DataFrame:
    """模拟净值数据缺失第 2 天（测试 ffill）。"""
    dates = pd.to_datetime(["2024-01-02", "2024-01-04"])
    return pd.DataFrame(
        {
            "UNIT_NAV": [1.0, 1.05],
            "PRICE_DATE": [20240102, 20240104],
            "ANN_DATE": [20240102, 20240104],
        },
        index=pd.Index(dates, name="date"),
    )


# ========== test_filter_broad_based ==========

def test_filter_broad_based():
    """验证关键词匹配：含宽基关键词且不含排除关键词的 ETF 被筛入。"""
    df = make_code_info_df()
    result = EtfFlowService._filter_broad_based(df)
    codes = {c for c, _ in result}
    names = {n for _, n in result}

    # 沪深300ETF / 中证500ETF / 创业板指ETF 被纳入
    assert "510300.SH" in codes
    assert "510500.SH" in codes
    assert "159915.SZ" in codes
    # 沪深300增强ETF 被排除（含"增强"）
    assert "510301.SH" not in codes
    # 医药ETF 被排除（含"医药"）
    assert "159999.SZ" not in codes
    # 空简称被跳过
    assert "510302.SH" not in codes
    # 确认返回的 name 正确
    assert "沪深300ETF" in names
    assert "中证500ETF" in names


def test_filter_broad_based_empty_df():
    """空 DataFrame → 返回空列表。"""
    df = pd.DataFrame()
    assert EtfFlowService._filter_broad_based(df) == []


# ========== test_normalize_share_df 深市日期修正 ==========

def test_normalize_share_df_sh_no_shift():
    """沪市 ETF：CHANGE_DATE != ANN_DATE（差一天），CHANGE_DATE 可信，不修正。"""
    df = pd.DataFrame({
        "FUND_SHARE": [100.0, 120.0],
        "CHANGE_DATE": [20240102, 20240103],
        "ANN_DATE": [20240103, 20240104],  # 沪市 ANN_DATE = CHANGE_DATE + 1
    })
    result = EtfFlowService._normalize_share_df(df, code="510300.SH")
    # date 保持 CHANGE_DATE 原值，不减 1 天
    assert result["date"].tolist() == ["2024-01-02", "2024-01-03"]
    assert result["share"].tolist() == [100.0, 120.0]


def test_normalize_share_df_sz_shift_back_one_day():
    """深市 ETF：CHANGE_DATE == ANN_DATE（均填公告日），无日历时减 1 天。"""
    df = pd.DataFrame({
        "FUND_SHARE": [100.0, 120.0],
        "CHANGE_DATE": [20240103, 20240104],  # 深市：CHANGE_DATE == ANN_DATE = 公告日
        "ANN_DATE": [20240103, 20240104],
    })
    result = EtfFlowService._normalize_share_df(df, code="159915.SZ")
    # 无日历时退化为减 1 天：20240103→20240102, 20240104→20240103
    assert result["date"].tolist() == ["2024-01-02", "2024-01-03"]
    assert result["share"].tolist() == [100.0, 120.0]


def test_normalize_share_df_sz_snap_with_calendar():
    """深市 ETF：有日历时 snap 到前一交易日（跳过周末）。

    模拟真实场景：CHANGE_DATE=20260720（周一，公告日），前一交易日是 7/17（周五）。
    日历含 [..., 20260717, 20260718(周六不在), 20260719(周日不在), 20260720, ...]
    snap(20260720) → 20260717（小于 20260720 的最大交易日）
    """
    calendar = [20260716, 20260717, 20260720, 20260721, 20260722]  # 不含周末
    df = pd.DataFrame({
        "FUND_SHARE": [1302445.49, 1492045.49],
        "CHANGE_DATE": [20260720, 20260721],  # 公告日（周一、周二）
        "ANN_DATE": [20260720, 20260721],
    })
    result = EtfFlowService._normalize_share_df(df, code="159915.SZ", calendar=calendar)
    # 20260720 → snap 到前一交易日 20260717（周五）
    # 20260721 → snap 到前一交易日 20260720（周一）
    assert result["date"].tolist() == ["2026-07-17", "2026-07-20"]
    assert result["share"].tolist() == [1302445.49, 1492045.49]


def test_normalize_share_df_sz_no_shift_when_dates_differ():
    """深市但 CHANGE_DATE != ANN_DATE 时不修正（异常数据保护，不误改）。"""
    df = pd.DataFrame({
        "FUND_SHARE": [100.0, 120.0],
        "CHANGE_DATE": [20240102, 20240103],
        "ANN_DATE": [20240103, 20240104],  # CHANGE_DATE != ANN_DATE
    })
    result = EtfFlowService._normalize_share_df(df, code="159915.SZ")
    # 虽然 code 是 .SZ，但 CHANGE_DATE != ANN_DATE，说明 CHANGE_DATE 已是变动日，不修正
    assert result["date"].tolist() == ["2024-01-02", "2024-01-03"]


def test_compute_net_inflow_sz_date_alignment():
    """深市 ETF 端到端：share CHANGE_DATE（公告日）修正后与 nav PRICE_DATE 对齐。

    模拟深市真实场景：
    - share: CHANGE_DATE=20240103(=ANN_DATE，公告日), FUND_SHARE=120 → snap 到 20240102
    - nav:   PRICE_DATE=20240102, UNIT_NAV=1.05（1/2 交易日的净值）
    - 修正后 share date=20240102 与 nav date=20240102 对齐，净流入 = (120-100)*1.05 = 21
    """
    calendar = [20240101, 20240102, 20240103, 20240104]
    codes = ["159915.SZ"]
    name_map = {"159915.SZ": "创业板"}
    # 2 天 share 数据，深市 CHANGE_DATE == ANN_DATE
    share_dict = {"159915.SZ": pd.DataFrame({
        "FUND_SHARE": [100.0, 120.0],
        "CHANGE_DATE": [20240102, 20240103],  # 深市公告日，实际变动日应为前一个交易日
        "ANN_DATE": [20240102, 20240103],
    })}
    # nav 2 天，PRICE_DATE 是交易日
    nav_dict = {"159915.SZ": pd.DataFrame({
        "UNIT_NAV": [1.0, 1.05],
        "PRICE_DATE": [20240101, 20240102],  # 净值交易日
        "ANN_DATE": [20240102, 20240103],
    })}
    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None, calendar
    )
    # 修正后 share date = [2024-01-01, 2024-01-02]，与 nav PRICE_DATE 对齐
    assert len(records) == 2
    assert records[0]["date"] == "2024-01-01"
    assert records[1]["date"] == "2024-01-02"
    # 第 2 天份额变动 = 120 - 100 = 20，净值 = 1.05，净流入 = 21
    assert records[1]["net_inflow_share"] == 20.0
    assert records[1]["net_inflow_amount"] == pytest.approx(21.0)
    assert records[1]["nav"] == 1.05


def test_filter_broad_based_numeric_patterns():
    """纯数字/代号简称（300ETF/A500/HS300 等）应被纳入,但增强/指增后缀应排除。"""
    df = pd.DataFrame(
        {
            "symbol": [
                "300ETF",       # 数字模式 → 纳入
                "500ETF",       # 数字模式 → 纳入
                "1000ETF",      # 数字模式 → 纳入
                "A500",         # 数字模式 → 纳入
                "A50ETF",       # 数字模式 → 纳入
                "HS300",        # 数字模式 → 纳入
                "ZZA500",       # 数字模式 → 纳入
                "50科创",       # 数字模式 → 纳入
                "D100ETF",      # 数字模式 → 纳入
                "SH50ETF",      # 数字模式 → 纳入
                "300基金",      # 数字模式 → 纳入
                "180E",         # 数字模式 → 纳入
                # 新增全称关键词覆盖
                "创业板",       # "创业板"关键词 → 纳入
                "科创板50",     # "科创板50"关键词 → 纳入
                "双创基金",     # "双创基金"关键词 → 纳入
                "创业五零",     # "创业五零"关键词 → 纳入
                "上证综合",     # "上证综合"关键词 → 纳入
                "综指ETF",      # "综指ETF"关键词 → 纳入
                "华夏300",      # "华夏300"关键词 → 纳入
                "天弘300",      # "天弘300"关键词 → 纳入
                "景顺A500",     # "景顺A500"关键词 → 纳入
                "科创富国",     # "科创富国"关键词 → 纳入
                "科创平安",     # "科创平安"关键词 → 纳入
                # 排除边界
                "300增强",      # 数字模式 + 排除词"增强" → 排除
                "500指增",      # 数字模式 + 后缀"指增" → 排除
                "300指数",      # 数字模式 + 后缀"指数" → 排除
                "300价值",      # 数字模式 + 排除词"价值" → 排除
                "创业板增",     # "创业板"命中 + 排除词"创业板增" → 排除
                "创业板综",     # "创业板"命中 + 排除词"创业板综" → 排除
                # 非宽基
                "225ETF",       # 日经225,不在数字模式白名单 → 不纳入
                "10年地债",     # 债券,不匹配 → 不纳入
                "100ETF",       # 中证100,不在宽基清单 → 不纳入
            ],
        },
        index=pd.Index(
            ["510300.SH", "510500.SH", "512100.SH", "159339.SZ",
             "512150.SH", "515390.SH", "159359.SZ", "588840.SH",
             "159923.SZ", "530050.SH", "159330.SZ", "530680.SH",
             "159915.SZ", "588080.SH", "159783.SZ", "159682.SZ",
             "510980.SH", "510210.SH", "510330.SH", "515330.SH",
             "159353.SZ", "588940.SH", "589150.SH",
             "561990.SH", "561550.SH", "560610.SH", "562320.SH",
             "159676.SZ", "159541.SZ",
             "513000.SH", "511270.SH", "512910.SH"],
            name="code",
        ),
    )
    result = EtfFlowService._filter_broad_based(df)
    codes = {c for c, _ in result}

    # 纯数字宽基简称被纳入
    assert "510300.SH" in codes   # 300ETF
    assert "510500.SH" in codes   # 500ETF
    assert "512100.SH" in codes   # 1000ETF
    assert "159339.SZ" in codes   # A500
    assert "512150.SH" in codes   # A50ETF
    assert "515390.SH" in codes   # HS300
    assert "159359.SZ" in codes   # ZZA500
    assert "588840.SH" in codes   # 50科创
    assert "159923.SZ" in codes   # D100ETF
    assert "530050.SH" in codes   # SH50ETF
    assert "159330.SZ" in codes   # 300基金
    assert "530680.SH" in codes   # 180E
    # 新增全称关键词被纳入
    assert "159915.SZ" in codes   # 创业板
    assert "588080.SH" in codes   # 科创板50
    assert "159783.SZ" in codes   # 双创基金
    assert "159682.SZ" in codes   # 创业五零
    assert "510980.SH" in codes   # 上证综合
    assert "510210.SH" in codes   # 综指ETF
    assert "510330.SH" in codes   # 华夏300
    assert "515330.SH" in codes   # 天弘300
    assert "159353.SZ" in codes   # 景顺A500
    assert "588940.SH" in codes   # 科创富国
    assert "589150.SH" in codes   # 科创平安
    # 增强类/指增类/综合类被排除
    assert "561990.SH" not in codes  # 300增强
    assert "561550.SH" not in codes  # 500指增
    assert "560610.SH" not in codes  # 300指数
    assert "562320.SH" not in codes  # 300价值
    assert "159676.SZ" not in codes  # 创业板增
    assert "159541.SZ" not in codes  # 创业板综
    # 非宽基(日经/债券/中证100)不纳入
    assert "513000.SH" not in codes  # 225ETF
    assert "511270.SH" not in codes  # 10年地债
    assert "512910.SH" not in codes  # 100ETF


def test_filter_broad_based_none_df():
    """None → 返回空列表。"""
    assert EtfFlowService._filter_broad_based(None) == []


# ========== test_compute_net_inflow ==========

def test_compute_net_inflow_share_unchanged():
    """份额不变 → net_inflow_share=0, net_inflow_amount=0。"""
    codes = ["510300.SH"]
    name_map = {"510300.SH": "沪深300ETF"}
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}

    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None
    )
    assert len(records) == 3
    # 第 1 天 diff() 为 NaN → serialize → None
    assert records[0]["net_inflow_share"] is None
    assert records[0]["net_inflow_amount"] is None
    # 第 2 天份额不变 → 0
    assert records[1]["net_inflow_share"] == 0.0
    assert records[1]["net_inflow_amount"] == 0.0
    # 第 3 天份额增加 20 万份
    assert records[2]["net_inflow_share"] == 20.0
    assert records[2]["net_inflow_amount"] == pytest.approx(20.0 * 1.05)


def test_compute_net_inflow_share_increase():
    """份额增加 → net_inflow_share > 0, net_inflow_amount > 0。"""
    codes = ["510300.SH"]
    name_map = {"510300.SH": "沪深300ETF"}
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}

    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None
    )
    # 第 3 天: 120-100=20 万份, 净值 1.05
    assert records[2]["net_inflow_share"] == 20.0
    assert records[2]["net_inflow_amount"] == pytest.approx(21.0)


def test_compute_net_inflow_first_day_none():
    """首日 diff() 为 NaN → 经 serialize_dataframe 转为 None。"""
    codes = ["510300.SH"]
    name_map = {"510300.SH": "沪深300ETF"}
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}

    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None
    )
    assert records[0]["net_inflow_share"] is None
    assert records[0]["net_inflow_amount"] is None


def test_compute_net_inflow_nav_ffill():
    """净值缺失中间日 → ffill 用前值填充。"""
    codes = ["510300.SH"]
    name_map = {"510300.SH": "沪深300ETF"}
    share_dict = {"510300.SH": make_share_df()}
    # 净值缺第 2 天（只有第 1 天和第 3 天）
    nav_dict = {"510300.SH": make_nav_df_missing()}

    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None
    )
    assert len(records) == 3
    # 第 2 天净值通过 ffill 用第 1 天的 1.0
    assert records[1]["nav"] == 1.0
    # 第 2 天份额不变 → 0
    assert records[1]["net_inflow_share"] == 0.0
    assert records[1]["net_inflow_amount"] == 0.0


def test_compute_net_inflow_no_nav():
    """净值 DataFrame 为空 → nav 列全 None，net_inflow_amount 全 None。"""
    codes = ["510300.SH"]
    name_map = {"510300.SH": "沪深300ETF"}
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {}  # 无净值数据

    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None
    )
    assert len(records) == 3
    for r in records:
        assert r["nav"] is None
        assert r["net_inflow_amount"] is None


def test_compute_net_inflow_empty_share():
    """份额数据为空 → 返回空列表。"""
    codes = ["510300.SH"]
    name_map = {"510300.SH": "沪深300ETF"}
    share_dict = {}  # 无份额数据
    nav_dict = {"510300.SH": make_nav_df()}

    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, None, None
    )
    assert records == []


# ========== test_query_full_flow ==========

def test_query_full_flow():
    """FakeGateway 注入 mock code_info + fund_share + fund_nav → 端到端验证。"""
    code_info_df = make_code_info_df()
    share_dict = {
        "510300.SH": make_share_df(),
        "510500.SH": make_share_df(),
        "159915.SZ": make_share_df(),
    }
    nav_dict = {
        "510300.SH": make_nav_df(),
        "510500.SH": make_nav_df(),
        "159915.SZ": make_nav_df(),
    }
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    # 传日期匹配 mock 数据(2024-01-02 ~ 2024-01-04)
    # 不传时默认近 30 天,mock 数据日期会被过滤
    records = svc.query(start_time="2024-01-01", end_time="2024-01-31")

    # 3 只宽基 × 3 天 = 9 条记录
    assert len(records) == 9
    codes = {r["code"] for r in records}
    assert codes == {"510300.SH", "510500.SH", "159915.SZ"}
    # 验证字段
    for r in records:
        assert set(r.keys()) == {
            "code", "name", "date", "share", "nav",
            "net_inflow_share", "net_inflow_amount",
        }


def test_query_passes_dates_to_gateway():
    """query 传 start/end → get_fund_share/get_fund_nav 收到 SDK 8 位日期。"""
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    svc.query("2024-01-02", "2024-01-04")

    # 验证 get_fund_share 被传入 begin_date=20240102, end_date=20240104
    assert gw.fund_share_calls[0]["begin_date"] == 20240102
    assert gw.fund_share_calls[0]["end_date"] == 20240104
    assert gw.fund_nav_calls[0]["begin_date"] == 20240102
    assert gw.fund_nav_calls[0]["end_date"] == 20240104


def test_query_extends_fetch_range_with_calendar():
    """有日历时 query 前扩 1 交易日、后扩 10 天拉数据,返回结果仍用原始区间过滤。

    日历 [20240102, 20240103, 20240104, 20240105], 用户区间 20240103~20240104:
    - 前扩: bisect_left(20240103)=1 → idx>=2 为 False, idx==1 → fetch_begin=cal_sorted[0]=20240102
    - 后扩: 20240104 + 10 天 = 20240114
    """
    calendar = [20240102, 20240103, 20240104, 20240105]
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
        calendar=calendar,
    )
    svc = EtfFlowService(gw)
    svc.query("2024-01-03", "2024-01-04")

    # fetch 区间: 前扩 1 交易日(20240102), 后扩 10 天(20240114)
    assert gw.fund_share_calls[0]["begin_date"] == 20240102
    assert gw.fund_share_calls[0]["end_date"] == 20240114
    assert gw.fund_nav_calls[0]["begin_date"] == 20240102
    assert gw.fund_nav_calls[0]["end_date"] == 20240114


def test_query_extends_fetch_end_when_calendar_stale():
    """日历滞后（end_date >= 日历最后一天）时,后扩仍用 end_date+10 天,和正常情况一致。

    模拟真实场景：日历只到 20260731,用户 end_time=2026-08-01,
    深市 7/31 的份额数据 8/3 才公告,需 fetch_end 扩到 8/11 才能拉到。
    """
    calendar = [20260729, 20260730, 20260731]  # 日历最后一天 7/31,不含 8 月
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
        calendar=calendar,
    )
    svc = EtfFlowService(gw)
    svc.query("2026-07-30", "2026-08-01")

    # end_date=20260801 + 10 天 = 20260811
    assert gw.fund_share_calls[0]["end_date"] == 20260811
    assert gw.fund_nav_calls[0]["end_date"] == 20260811


def test_query_no_overexpand_for_historical_dates():
    """查历史数据时（end_date 远早于日历最后一天）,后扩仍只加 10 天,不会拉全量。

    日历含 2024 和 2026 的日期,用户查 2024-01-03~2024-01-04:
    - 后扩 10 天 = 20240114,不会拉到 2026 年的数据。
    """
    calendar = [20240102, 20240103, 20240104, 20240105, 20260729, 20260730, 20260731]
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
        calendar=calendar,
    )
    svc = EtfFlowService(gw)
    svc.query("2024-01-03", "2024-01-04")

    # end_date=20240104 + 10 天 = 20240114（不会拉到 2026 年）
    assert gw.fund_share_calls[0]["end_date"] == 20240114
    assert gw.fund_nav_calls[0]["end_date"] == 20240114


# ========== test_date_filter ==========

def test_date_filter_start_only():
    """start_time 过滤掉早于该日期的行。"""
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    records = svc.query("2024-01-03")
    assert len(records) == 2  # 1-03, 1-04
    for r in records:
        assert r["date"] >= "2024-01-03"


def test_date_filter_end_only():
    """end_time 过滤掉晚于该日期的行。"""
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    records = svc.query(None, "2024-01-03")
    assert len(records) == 2  # 1-02, 1-03
    for r in records:
        assert r["date"] <= "2024-01-03"


def test_date_filter_both_sides():
    """start+end 双侧过滤。"""
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    records = svc.query("2024-01-03", "2024-01-03")
    assert len(records) == 1
    assert records[0]["date"] == "2024-01-03"


# ========== test_empty_result ==========

def test_empty_result_no_broad_based():
    """宽基列表为空 → 返回 []。"""
    # 全部是非宽基 ETF
    code_info_df = pd.DataFrame(
        {"symbol": ["医药ETF", "券商ETF", "银行ETF"]},
        index=pd.Index(["159999.SZ", "512000.SH", "512800.SH"], name="code"),
    )
    gw = FakeGateway(ready=True, code_info_result=code_info_df)
    svc = EtfFlowService(gw)
    assert svc.query() == []


def test_empty_result_no_share_data():
    """宽基列表非空但份额数据为空 → 返回 []。"""
    code_info_df = make_code_info_df()
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result={},  # 空份额
        fund_nav_result={},
    )
    svc = EtfFlowService(gw)
    assert svc.query() == []


def test_empty_result_code_info_none():
    """get_code_info 返回 None → 返回 []。"""
    gw = FakeGateway(ready=True, code_info_result=None)
    svc = EtfFlowService(gw)
    assert svc.query() == []


# ========== test_start_after_end ==========

def test_start_after_end_raises():
    """start > end → ValueError。"""
    gw = FakeGateway(ready=True, code_info_result=make_code_info_df())
    svc = EtfFlowService(gw)
    with pytest.raises(ValueError, match="start_time must not be later than end_time"):
        svc.query("2024-01-04", "2024-01-02")


def test_invalid_date_format_raises():
    """日期格式错误 → ValueError。"""
    gw = FakeGateway(ready=True, code_info_result=make_code_info_df())
    svc = EtfFlowService(gw)
    with pytest.raises(ValueError):
        svc.query("2024/01/02")


def test_iso_datetime_accepted():
    """ISO datetime 格式应被接受。"""
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    records = svc.query("2024-01-02T00:00:00", "2024-01-02T23:59:59")
    assert len(records) == 1
    assert records[0]["date"] == "2024-01-02"


def test_sdk_not_ready():
    """SDK 未就绪 → GatewayNotReadyError。"""
    gw = FakeGateway(ready=False)
    svc = EtfFlowService(gw)
    with pytest.raises(GatewayNotReadyError):
        svc.query()


# ========== test_cache ==========

def test_etf_flow_result_cache_hit_skips_gateway():
    """同参数第二次查询零 gateway 调用（结果缓存命中）。"""
    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)
    # 第一次查询 → gateway 被调用
    records1 = svc.query("2024-01-01", "2024-01-31")
    assert len(records1) == 3  # 1 只宽基 × 3 天
    assert len(gw.fund_share_calls) == 1

    # 第二次同参数查询 → 结果缓存命中，零 gateway 调用
    records2 = svc.query("2024-01-01", "2024-01-31")
    assert records2 == records1
    assert len(gw.fund_share_calls) == 1  # 仍为 1，未新增调用


def test_etf_flow_list_cache_expires_next_day(monkeypatch):
    """清单缓存按日失效（跨日重取 get_code_info）。"""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    code_info_df = make_code_info_df()
    share_dict = {"510300.SH": make_share_df()}
    nav_dict = {"510300.SH": make_nav_df()}
    gw = FakeGateway(
        ready=True,
        code_info_result=code_info_df,
        fund_share_result=share_dict,
        fund_nav_result=nav_dict,
    )
    svc = EtfFlowService(gw)

    # 用不同 start_time/end_time 避免命中结果缓存，只测清单缓存
    # 第一天：now_cn 返回 2024-01-15
    day1 = _dt.datetime(2024, 1, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("app.etf_flow_service.now_cn", lambda: day1)
    svc.query("2024-01-01", "2024-01-04")
    assert len(gw.fund_share_calls) == 1

    # 第二天：now_cn 返回 2024-01-16（跨日），清单缓存失效
    day2 = _dt.datetime(2024, 1, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("app.etf_flow_service.now_cn", lambda: day2)
    svc.query("2024-01-01", "2024-01-05")  # 不同区间避开结果缓存，只测清单缓存
    # 跨日后清单缓存失效，get_code_info 被重新调用
    # 通过 fund_share_calls 增加 1 来验证走了完整查询路径
    assert len(gw.fund_share_calls) == 2

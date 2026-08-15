"""Probe: 用 get_code_info 动态获取全量 ETF + 关键词匹配宽基,拉份额+净值计算净流入。

完整走宽基筛选+净流入计算的真实链路(除了 get_code_info 直接调 SDK 而非 gateway)。
宽基筛选常量与逻辑自包含（从已删除的 EtfFlowService 复制），不 import 任何已删除符号。

运行：python -u scripts/probe_etf_dynamic.py
"""
import os, sys, time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ========== 宽基筛选常量+逻辑（自包含，从旧 EtfFlowService 复制） ==========

# 宽基指数关键词白名单：简称含任一即视为宽基候选
BROAD_BASED_KEYWORDS = [
    "上证50", "沪深300", "中证500", "中证800", "中证1000", "中证2000",
    "创业板50", "创业板指", "创业板", "创业五零",  # "创业板"覆盖 159915/创业板TH/HX 等
    "科创50", "科创100", "科创创业50", "科创板50",  # "科创板50"覆盖 588080
    "双创50", "双创基金",  # "双创基金"覆盖 159783
    "上证180", "深证100", "深证300", "上证380", "中证全指",
    "中证A50", "中证A500", "国证2000",
    "上证综合", "综指ETF",  # 上证综指 ETF（510980/510210）
    # 基金公司+指数名 命名模式（公司简称在前，指数代号在后）
    "华夏300", "天弘300", "景顺A500",
    "科创富国", "科创平安",  # 科创50 的公司前缀变体（588940/589150）
]

# 纯数字/代号简称正则：匹配省略"沪深/中证"前缀的宽基 ETF 简称
NUMERIC_PATTERNS = [
    r"^50ETF",          # 50ETF / 50ETF基 / SH50ETF / SZ50ETF
    r"^50科创",         # 50科创(科创50 反写)
    r"^180ETF",         # 180ETF / 180ETF指(后缀"指"不在排除,但"指基/指数"在)
    r"^180E$",          # 180E(E 份额变体,单独字母后缀)
    r"^180基金",        # 180基金
    r"^300ETF",         # 300ETF / 300ETF增
    r"^300基金",        # 300基金
    r"^380ETF",         # 380ETF
    r"^500ETF",         # 500ETF / 500ETFEW / 500ETFFG / 500ETF基
    r"^500基金",        # 500基金
    r"^800ETF",         # 800ETF
    r"^800基金",        # 800基金(如有)
    r"^1000ETF",        # 1000ETF
    r"^1000基金",       # 1000基金
    r"^2000ETF",        # 2000ETF
    r"^2000E$",         # 2000E(E 份额变体)
    r"^2000基金",       # 2000基金
    r"^ETF500",         # ETF500
    r"^D100ETF",        # D100ETF(深证100 代号)
    r"^HS300",          # HS300 / HS300E / HS300ETF / HGS300
    r"^HGS300",         # HGS300(港股沪深300)
    r"^ZZ100$",         # ZZ100(中证100 纯代号)
    r"^ZZ500",          # ZZ500ETF
    r"^ZZ800",          # ZZ800ETF
    r"^ZZ1000",         # ZZ1000
    r"^ZZ2000",         # ZZ2000
    r"^ZZA500",         # ZZA500(中证A500 代号)
    r"^CSI2000",        # CSI2000
    r"^GZ2000",         # GZ2000(国证2000 代号)
    r"^AH300ETF",       # AH300ETF
    r"^AH500ETF",       # AH500ETF
    r"^HGS500",         # HGS500 / HGS500E
    r"^MSCIA50",        # MSCIA50
    r"^SH50ETF",        # SH50ETF
    r"^SZ50ETF",        # SZ50ETF
    r"^A50",            # A50 / A50ETF / A50中证 / A50华宝 / A50基金 / A50指数 / A50龙头
    r"^A100",           # A100 / A100ETF / A100基金 / A100指基 / A100指数
    r"^A500",           # A500 / A500ETF / A500E / A500DC / A500万家 / ... 全族
    r"^BOCI500",        # BOCI500
]

# 排除关键词：简称含任一即排除（避免误纳入行业/主题/策略/增强 ETF）
EXCLUDE_KEYWORDS = [
    "增强", "策略", "量价", "行业", "主题", "红利", "低波", "价值", "成长",
    "质量", "动量", "基本面", "ESG", "消费", "医药", "科技", "金融", "能源",
    "新能源", "半导体", "军工", "基建", "材料", "工业", "通信", "环保",
    "食品", "白酒", "房地产", "银行", "券商", "保险", "汽车", "农业", "旅游",
    "创新", "龙头",  # 风格/主题因子
    "创业板增", "创业板综",  # 防止"创业板"关键词误收增强/综合类衍生品
]

# 排除后缀：数字代号简称匹配后,若以这些后缀结尾则排除（指增/增强等衍生产品）
EXCLUDE_SUFFIXES = [
    "增", "增强", "指增", "增指", "指基", "指数", "现金",  # 增强类/机构定制
]


def _filter_broad_based(df):
    """从 ETF DataFrame 筛选宽基 ETF，返回 [(code, name), ...]。

    匹配规则（任一命中即视为宽基候选，再经排除词过滤）：
    1. 简称含任一 BROAD_BASED_KEYWORDS（全称，如"沪深300"）
    2. 简称匹配任一 NUMERIC_PATTERNS（纯数字/代号简称，如"300ETF"/"A500"）

    排除规则（命中任一即排除）：
    - 简称含任一 EXCLUDE_KEYWORDS（行业/主题/风格因子）
    - 简称以任一 EXCLUDE_SUFFIXES 结尾（仅对 NUMERIC_PATTERNS 命中的二次过滤,
      排除"xxx增/xxx指增/xxx指数"等增强类衍生产品）

    简称为空/NaN → 跳过。

    向量化实现：用 str.contains / str.match 正则一次匹配全部，避免 iterrows。
    """
    import pandas as pd

    if df is None or df.empty:
        return []

    names = df.get("symbol")
    if names is None:
        return []
    names = names.astype(str)
    # 跳过空/nan
    valid = (names != "nan") & (names != "") & names.notna()
    names_valid = names[valid]

    # 命中宽基：全称关键词 OR 数字代号模式
    broad_pattern = "|".join(BROAD_BASED_KEYWORDS)
    numeric_pattern = "|".join(f"(?:{p})" for p in NUMERIC_PATTERNS)
    has_broad = names_valid.str.contains(broad_pattern, na=False) | \
                names_valid.str.match(numeric_pattern, na=False)

    # 排除关键词：行业/主题/风格因子（全称和数字模式统一过滤）
    exclude_pattern = "|".join(EXCLUDE_KEYWORDS)
    has_exclude = names_valid.str.contains(exclude_pattern, na=False)

    # 排除后缀：仅对"数字代号命中但非全称命中"的简称做二次过滤
    only_numeric = has_broad & ~names_valid.str.contains(broad_pattern, na=False)
    suffix_pattern = "|".join(f"(?:{s})$" for s in EXCLUDE_SUFFIXES)
    has_suffix_exclude = only_numeric & names_valid.str.contains(suffix_pattern, na=False)

    selected = names_valid[has_broad & ~has_exclude & ~has_suffix_exclude]

    return [(str(idx), str(name)) for idx, name in selected.items()]


_T0 = time.monotonic()
def _log(msg):
    print(f"[probe +{time.monotonic() - _T0:6.1f}s] {msg}", flush=True)


def main():
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    import AmazingData as ad
    _log("import OK")
    ad.login(username=os.environ["AMAZINGDATA_USERNAME"], password=os.environ["AMAZINGDATA_PASSWORD"],
             host=os.environ["AMAZINGDATA_HOST"], port=int(os.environ["AMAZINGDATA_PORT"]))
    _log("login OK")

    base = ad.BaseData()
    info = ad.InfoData()
    calendar = base.get_calendar()
    today = calendar[-1]
    _log(f"today={today}")

    # ========== 1. 动态获取全量 ETF ==========
    _log("calling get_code_info('EXTRA_ETF') ...")
    t0 = time.perf_counter()
    etf_df = base.get_code_info(security_type="EXTRA_ETF")
    t1 = time.perf_counter()
    total_count = len(etf_df) if etf_df is not None else 0
    _log(f"get_code_info OK: {total_count} 只 ETF in {t1-t0:.1f}s")

    # ========== 2. 宽基筛选 ==========
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.fund_data_service import FundDataService

    broad_based = _filter_broad_based(etf_df)
    _log(f"宽基筛选: {len(broad_based)}/{total_count} 只被选中")

    print(f"\n{'='*80}", flush=True)
    print(f"全量 ETF: {total_count} 只 → 宽基 ETF: {len(broad_based)} 只", flush=True)
    print(f"{'='*80}", flush=True)

    # 打印全部宽基 ETF
    for i, (code, name) in enumerate(sorted(broad_based)):
        print(f"  {i+1:4d}. {code}  {name}", flush=True)

    if not broad_based:
        print("WARN: 没有选中任何宽基 ETF", flush=True)
        try: ad.logout(username=os.environ["AMAZINGDATA_USERNAME"])
        except: pass
        return 0

    codes = [c for c, _ in broad_based]
    name_map = {c: n for c, n in broad_based}

    # ========== 3. 拉份额+净值(近 30 天, 前后各扩 1 交易日) ==========
    now = datetime.now()
    begin_date = int((now - timedelta(days=30)).strftime("%Y%m%d"))
    end_date = int(now.strftime("%Y%m%d"))
    _log(f"用户日期范围: {begin_date} ~ {end_date}, codes={len(codes)}")

    # 扩展拉取区间：前扩 1 交易日（bisect），后扩 10 天（覆盖深市 T+1 公告跨长假）
    import bisect
    cal_sorted = sorted(calendar)
    idx = bisect.bisect_left(cal_sorted, begin_date)
    fetch_begin = cal_sorted[max(0, idx - 2)] if idx >= 2 else (cal_sorted[0] if cal_sorted else begin_date)
    from datetime import datetime as _dt, timedelta as _td
    _e = _dt.strptime(str(end_date), "%Y%m%d")
    fetch_end = int((_e + _td(days=10)).strftime("%Y%m%d"))
    _log(f"扩展拉取区间: {fetch_begin} ~ {fetch_end} (前后各扩 1 交易日)")

    local_path = str(PROJECT_ROOT / "data") + "/"
    Path(local_path).mkdir(parents=True, exist_ok=True)

    _log("calling get_fund_share ...")
    t0 = time.perf_counter()
    share_dict = info.get_fund_share(codes, local_path=local_path, is_local=False,
                                     begin_date=fetch_begin, end_date=fetch_end)
    t1 = time.perf_counter()
    _log(f"get_fund_share OK: {len(share_dict)} codes in {t1-t0:.1f}s")

    _log("calling get_fund_nav ...")
    t0 = time.perf_counter()
    nav_dict = info.get_fund_nav(codes, local_path=local_path, is_local=False,
                                 begin_date=fetch_begin, end_date=fetch_end)
    t1 = time.perf_counter()
    _log(f"get_fund_nav OK: {len(nav_dict)} codes in {t1-t0:.1f}s")

    # ========== 4. 计算净流入 ==========
    start_dt = datetime.strptime(str(begin_date), "%Y%m%d")
    end_dt = datetime.strptime(str(end_date), "%Y%m%d")
    t0 = time.perf_counter()
    records = []
    for code in codes:
        raw_share = share_dict.get(code)
        if raw_share is None or raw_share.empty:
            continue
        share_df = FundDataService._normalize_share_df(raw_share, code, calendar)
        raw_nav = nav_dict.get(code)
        nav_df = FundDataService._normalize_nav_df(raw_nav) if raw_nav is not None and not raw_nav.empty else None
        if nav_df is not None:
            merged = share_df.merge(nav_df, on="trade_date", how="left")
            merged["nav"] = merged["nav"].ffill()
        else:
            merged = share_df.copy()
            merged["nav"] = None
        merged["net_inflow_share"] = merged["share"].diff()
        merged["net_inflow_amount"] = merged["net_inflow_share"] * merged["nav"]
        merged = merged[(merged["trade_date"] >= start_dt.strftime("%Y-%m-%d"))
                        & (merged["trade_date"] <= end_dt.strftime("%Y-%m-%d"))]
        for _, row in merged.iterrows():
            records.append({"code": code, "name": name_map.get(code, ""), **row.to_dict()})
    t1 = time.perf_counter()
    _log(f"compute OK: {len(records)} records in {t1-t0:.1f}s")

    # ========== 5. 按日汇总 ==========
    by_date = defaultdict(list)
    for r in records:
        by_date[r["trade_date"]].append(r)

    print(f"\n{'='*90}", flush=True)
    print(f"近 30 天宽基 ETF 每日净流入汇总({len(records)} 条, {len(by_date)} 个交易日)", flush=True)
    print(f"{'='*90}", flush=True)
    print(f"{'日期':<12} {'ETF数':>6} {'流入':>6} {'流出':>6} {'净流入(万元)':>14} {'折合(亿元)':>10}", flush=True)
    print("-" * 70, flush=True)

    grand_total = 0.0
    for date in sorted(by_date.keys()):
        day_records = by_date[date]
        inflow_etfs = [r for r in day_records if r["net_inflow_amount"] is not None and r["net_inflow_amount"] > 0]
        outflow_etfs = [r for r in day_records if r["net_inflow_amount"] is not None and r["net_inflow_amount"] < 0]
        day_total = sum(r.get("net_inflow_amount") or 0 for r in day_records)
        grand_total += day_total
        print(f"{date:<12} {len(day_records):>6} {len(inflow_etfs):>6} {len(outflow_etfs):>6} {day_total:>14.2f} {day_total/10000:>10.2f}", flush=True)

    print("-" * 70, flush=True)
    print(f"{'合计':<12} {'':>6} {'':>6} {'':>6} {grand_total:>14.2f} {grand_total/10000:>10.2f}", flush=True)

    # ========== 6. 按 ETF 汇总排名 ==========
    print(f"\n{'='*80}", flush=True)
    print(f"近 30 天各宽基 ETF 累计净流入排名(Top 20)", flush=True)
    print(f"{'='*80}", flush=True)
    etf_totals = defaultdict(lambda: {"name": "", "total": 0.0, "days": 0})
    for r in records:
        code = r["code"]
        etf_totals[code]["name"] = r["name"]
        if r["net_inflow_amount"] is not None:
            etf_totals[code]["total"] += r["net_inflow_amount"]
            etf_totals[code]["days"] += 1

    ranked = sorted(etf_totals.items(), key=lambda x: x[1]["total"], reverse=True)
    print(f"{'代码':<14} {'简称':<20} {'累计净流入(万元)':>16} {'天数':>6}", flush=True)
    print("-" * 60, flush=True)
    highlight = {"159915.SZ", "510310.SH"}
    for i, (code, info) in enumerate(ranked):
        mark = " ★" if code in highlight else "  "
        print(f"{i+1:3d}.{mark}{code:<14} {info['name']:<20} {info['total']:>16.2f} {info['days']:>6}", flush=True)
    print("-" * 60, flush=True)

    try: ad.logout(username=os.environ["AMAZINGDATA_USERNAME"])
    except: pass
    _log("done")
    return 0

if __name__ == "__main__":
    sys.exit(main())

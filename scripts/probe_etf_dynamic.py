"""Probe: 用 get_code_info 动态获取全量 ETF + 关键词匹配宽基,拉份额+净值计算净流入。

完整走 EtfFlowService 的真实链路(除了 get_code_info 直接调 SDK 而非 gateway)。

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
    from app.etf_flow_service import EtfFlowService, BROAD_BASED_KEYWORDS, EXCLUDE_KEYWORDS

    broad_based = EtfFlowService._filter_broad_based(etf_df)
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
    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, start_dt, end_dt, calendar
    )
    t1 = time.perf_counter()
    _log(f"compute OK: {len(records)} records in {t1-t0:.1f}s")

    # ========== 5. 按日汇总 ==========
    by_date = defaultdict(list)
    for r in records:
        by_date[r["date"]].append(r)

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

"""Probe: 打印单只 ETF 近 30 天每日净流入明细。

分别测一只沪市 ETF（510300.SH 沪深300）和一只深市 ETF（159915.SZ 创业板）。

运行：python -u scripts/probe_etf_single.py
"""
import os, sys, time
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# 沪市 + 深市 各一只
TARGET_CODES = [
    ("510300.SH", "沪深300ETF（沪市）"),
    ("159915.SZ", "创业板ETF（深市）"),
]

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
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.etf_flow_service import EtfFlowService
    _log("import OK")
    ad.login(username=os.environ["AMAZINGDATA_USERNAME"], password=os.environ["AMAZINGDATA_PASSWORD"],
             host=os.environ["AMAZINGDATA_HOST"], port=int(os.environ["AMAZINGDATA_PORT"]))
    _log("login OK")

    base = ad.BaseData()
    info = ad.InfoData()
    calendar = base.get_calendar()
    today = calendar[-1]
    _log(f"today={today}")
    now = datetime.now()
    begin_date = int((now - timedelta(days=30)).strftime("%Y%m%d"))
    end_date = int(now.strftime("%Y%m%d"))

    local_path = str(PROJECT_ROOT / "data") + "/"
    Path(local_path).mkdir(parents=True, exist_ok=True)

    codes = [c for c, _ in TARGET_CODES]
    name_map = {c: n for c, n in TARGET_CODES}

    _log(f"拉份额 {codes} ...")
    share_dict = info.get_fund_share(codes, local_path=local_path, is_local=False,
                                     begin_date=begin_date, end_date=end_date)
    _log(f"份额 OK")

    _log(f"拉净值 {codes} ...")
    nav_dict = info.get_fund_nav(codes, local_path=local_path, is_local=False,
                                 begin_date=begin_date, end_date=end_date)
    _log(f"净值 OK")

    start_dt = datetime.strptime(str(begin_date), "%Y%m%d")
    end_dt = datetime.strptime(str(end_date), "%Y%m%d")
    records = EtfFlowService._compute_net_inflow(
        codes, name_map, share_dict, nav_dict, start_dt, end_dt, calendar
    )
    _log(f"计算 OK: {len(records)} 条")

    # 按代码分组
    by_code = {}
    for r in records:
        by_code.setdefault(r["code"], []).append(r)

    for code, label in TARGET_CODES:
        recs = sorted(by_code.get(code, []), key=lambda x: x["date"])
        print(f"\n{'='*90}", flush=True)
        print(f"{label}  {code}  近 30 天每日净流入（{len(recs)} 个交易日）", flush=True)
        print(f"{'='*90}", flush=True)
        print(f"{'日期':<12} {'份额(万份)':>14} {'净值':>10} {'份额变动(万份)':>16} {'净流入(万元)':>14} {'折合(亿)':>10}", flush=True)
        print("-" * 90, flush=True)

        total_amount = 0.0
        for r in recs:
            date = r["date"]
            share = r.get("share")
            nav = r.get("nav")
            delta = r.get("net_inflow_share")
            amount = r.get("net_inflow_amount")
            if amount is not None:
                total_amount += amount
            share_str = f"{share:,.2f}" if share is not None else "-"
            nav_str = f"{nav:.4f}" if nav is not None else "-"
            delta_str = f"{delta:+,.2f}" if delta is not None else "-"
            amt_str = f"{amount:+,.2f}" if amount is not None else "-"
            yi = f"{amount/10000:+.2f}" if amount is not None else "-"
            print(f"{date:<12} {share_str:>14} {nav_str:>10} {delta_str:>16} {amt_str:>14} {yi:>10}", flush=True)

        print("-" * 90, flush=True)
        print(f"{'合计':<12} {'':>14} {'':>10} {'':>16} {total_amount:>+14,.2f} {total_amount/10000:>+10.2f}", flush=True)

    try: ad.logout(username=os.environ["AMAZINGDATA_USERNAME"])
    except: pass
    _log("done")
    return 0

if __name__ == "__main__":
    sys.exit(main())

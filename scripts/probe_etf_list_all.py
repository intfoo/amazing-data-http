"""Probe: 拉全量 ETF 清单 + 宽基关键词命中标注,输出供人工核对遗漏。

输出结构：
  A. 命中宽基的 ETF（当前会被选中）
  B. 含宽基关键词但被排除词剔除的（边界，需确认排除是否正确）
  C. 名称以"字母+数字"开头的指数型简称但未命中（潜在遗漏候选，如 300ETF/A500ETF）
  D. 全量 ETF 清单（带 [Y]/[x]/[ ] 标注）

运行：python -u scripts/probe_etf_list_all.py > etf_all_list.txt 2>&1
"""
import os, sys, time
from pathlib import Path

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
    _log("calling get_code_info('EXTRA_ETF') ...")
    etf_df = base.get_code_info(security_type="EXTRA_ETF")
    total = len(etf_df) if etf_df is not None else 0
    _log(f"get_code_info OK: {total} 只")

    sys.path.insert(0, str(PROJECT_ROOT))
    from app.etf_flow_service import (
        BROAD_BASED_KEYWORDS, EXCLUDE_KEYWORDS,
        NUMERIC_PATTERNS, EXCLUDE_SUFFIXES, EtfFlowService,
    )

    # 用 service 层的真实筛选逻辑（已向量化）
    import pandas as pd
    names_series = etf_df["symbol"].astype(str)
    broad_pattern = "|".join(BROAD_BASED_KEYWORDS)
    numeric_pattern = "|".join(f"(?:{p})" for p in NUMERIC_PATTERNS)
    exclude_pattern = "|".join(EXCLUDE_KEYWORDS)
    suffix_pattern = "|".join(f"(?:{s})$" for s in EXCLUDE_SUFFIXES)

    names = names_series
    valid = (names != "nan") & (names != "") & names.notna()
    names_valid = names[valid]
    has_broad_word = names_valid.str.contains(broad_pattern, na=False)
    has_numeric = names_valid.str.match(numeric_pattern, na=False)
    has_broad = has_broad_word | has_numeric
    has_exclude = names_valid.str.contains(exclude_pattern, na=False)
    only_numeric = has_broad & ~has_broad_word
    has_suffix_exclude = only_numeric & names_valid.str.contains(suffix_pattern, na=False)
    matched = has_broad & ~has_exclude & ~has_suffix_exclude
    # 用 service 层方法取最终选中清单（确保与生产逻辑一致）
    broad_based = EtfFlowService._filter_broad_based(etf_df)
    matched_codes = {c for c, _ in broad_based}
    matched_mask = names_series.index.isin(matched_codes)

    # 被排除词剔除（命中宽基词/数字模式但被 EXCLUDE_KEYWORDS 挡掉）
    borderline_exclude = has_broad & has_exclude & ~has_suffix_exclude
    # 被后缀剔除（仅数字模式命中,被 EXCLUDE_SUFFIXES 挡掉）
    borderline_suffix = has_suffix_exclude
    # 数字型简称但完全没命中（潜在遗漏候选）
    idx_like = names_valid.str.match(r"^[A-Za-z]{0,6}\d{2,4}", na=False)
    candidates = idx_like & ~has_broad

    print(f"\n{'='*80}", flush=True)
    print("关键词配置", flush=True)
    print(f"{'='*80}", flush=True)
    print(f"BROAD_BASED_KEYWORDS ({len(BROAD_BASED_KEYWORDS)} 个):", flush=True)
    print("  " + " / ".join(BROAD_BASED_KEYWORDS), flush=True)
    print(f"NUMERIC_PATTERNS ({len(NUMERIC_PATTERNS)} 个, 正则):", flush=True)
    for p in NUMERIC_PATTERNS:
        print(f"  {p}", flush=True)
    print(f"EXCLUDE_KEYWORDS ({len(EXCLUDE_KEYWORDS)} 个):", flush=True)
    print("  " + " / ".join(EXCLUDE_KEYWORDS), flush=True)
    print(f"EXCLUDE_SUFFIXES ({len(EXCLUDE_SUFFIXES)} 个, 仅对数字模式):", flush=True)
    print("  " + " / ".join(EXCLUDE_SUFFIXES), flush=True)

    print(f"\n统计: 全量 {total} / 命中宽基 {len(broad_based)} / "
          f"被排除词剔除 {int(borderline_exclude.sum())} / "
          f"被后缀剔除 {int(borderline_suffix.sum())} / "
          f"数字型简称候选 {int(candidates.sum())}", flush=True)

    print(f"\n{'='*80}", flush=True)
    print(f"A. 命中宽基（{len(broad_based)} 只，当前会被选中）", flush=True)
    print(f"{'='*80}", flush=True)
    for i, (code, name) in enumerate(sorted(broad_based, key=lambda x: x[1])):
        print(f"  {i+1:4d}. {code:<14} {name}", flush=True)

    print(f"\n{'='*80}", flush=True)
    print(f"B1. 被排除词剔除（{int(borderline_exclude.sum())} 只，确认排除是否正确）", flush=True)
    print(f"{'='*80}", flush=True)
    rows = sorted(zip(names_valid.index[borderline_exclude].astype(str), names_valid[borderline_exclude]), key=lambda x: x[1])
    for i, (code, name) in enumerate(rows):
        hit = next((kw for kw in EXCLUDE_KEYWORDS if kw in name), "")
        print(f"  {i+1:4d}. {code:<14} {name:<24} [排除词: {hit}]", flush=True)

    print(f"\n{'='*80}", flush=True)
    print(f"B2. 被后缀剔除（{int(borderline_suffix.sum())} 只，指增/增强类衍生品）", flush=True)
    print(f"{'='*80}", flush=True)
    rows = sorted(zip(names_valid.index[borderline_suffix].astype(str), names_valid[borderline_suffix]), key=lambda x: x[1])
    for i, (code, name) in enumerate(rows):
        hit = next((s for s in EXCLUDE_SUFFIXES if name.endswith(s)), "")
        print(f"  {i+1:4d}. {code:<14} {name:<24} [后缀: {hit}]", flush=True)

    print(f"\n{'='*80}", flush=True)
    print(f"C. 数字型简称但未命中宽基词/模式（{int(candidates.sum())} 只，潜在遗漏候选）", flush=True)
    print(f"{'='*80}", flush=True)
    rows = sorted(zip(names_valid.index[candidates].astype(str), names_valid[candidates]), key=lambda x: x[1])
    for i, (code, name) in enumerate(rows):
        print(f"  {i+1:4d}. {code:<14} {name}", flush=True)

    print(f"\n{'='*80}", flush=True)
    print(f"D. 全量 ETF 清单（{total} 只）  [Y]=命中宽基 [x]=被排除词 [s]=被后缀剔除 [ ]=未命中", flush=True)
    print(f"{'='*80}", flush=True)
    # 构建 code -> mark 的快速查找表
    suffix_codes = set(names_valid.index[borderline_suffix].astype(str))
    exclude_codes = set(names_valid.index[borderline_exclude].astype(str))
    all_rows = sorted(zip(names_series.index.astype(str), names_series), key=lambda x: x[1])
    for i, (code, name) in enumerate(all_rows):
        if code in matched_codes:
            mark = "Y"
        elif code in suffix_codes:
            mark = "s"
        elif code in exclude_codes:
            mark = "x"
        else:
            mark = " "
        print(f"  [{mark}] {code:<14} {name}", flush=True)

    try: ad.logout(username=os.environ["AMAZINGDATA_USERNAME"])
    except: pass
    _log("done")
    return 0

if __name__ == "__main__":
    sys.exit(main())

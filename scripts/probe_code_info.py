"""Probe: 对比 get_code_info vs get_code_list 耗时。

get_code_list 全量拉取约 20s/次（股票+指数共 40s），启动慢。
手册 §3.5.2.1 get_code_info 返回 DataFrame（index=股票代码，含证券简称/涨跌停价等），
与 §3.5.2.2 get_code_list 同源（均"每日最新"）。本 probe 验证：
1. get_code_info 是否比 get_code_list 快（若快可用 df.index.tolist() 替代，额外拿简称）
2. 两者返回的代码集合是否一致（确认可替代）

自包含：读 .env 注入 env（同 probe_index_mixed.py）。
运行：python scripts/probe_code_info.py [--out docs/probe-code-info.json]
"""
import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = "docs/probe-code-info.json"
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env_creds():
    """读 .env 注入 os.environ 并返回凭据 dict（替代 local.config.json）。"""
    if not ENV_PATH.exists():
        return False
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
        return True
    except Exception:
        return False


def _finish(report, out_path):
    try:
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str, ensure_ascii=False)
    except OSError as e:
        print(f"probe: cannot write report {out_path}: {e}")
    print("==== probe summary ====")
    for key in ("get_code_list", "get_code_info"):
        entry = report.get(key, {})
        for st, data in entry.items():
            print(f"  {key}({st}): elapsed={data.get('elapsed_s')}s "
                  f"count={data.get('count')} ok={data.get('ok')}")
    cmp = report.get("comparison", {})
    if cmp:
        print("  comparison:")
        for k, v in cmp.items():
            print(f"    {k}: {v}")
    return 0


def _log(msg):
    print(f"[probe +{time.monotonic() - _T0:5.1f}s] {msg}", flush=True)


_T0 = time.monotonic()


def _timed(fn):
    """计时执行 fn()，返回 (result, elapsed_sec, error_str)。"""
    t0 = time.perf_counter()
    try:
        result = fn()
        return result, time.perf_counter() - t0, None
    except Exception as e:
        return None, time.perf_counter() - t0, f"{type(e).__name__}: {e}"


def probe(out_path=DEFAULT_OUT):
    report = {"errors": [], "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _log("start")

    if not _load_env_creds():
        report["errors"].append(".env not found or unreadable")
        return _finish(report, out_path)

    try:
        import AmazingData as ad
        report["sdk_version"] = getattr(ad, "__version__", "unknown")
        _log(f"import OK version={report['sdk_version']}")
    except Exception as e:
        report["errors"].append(f"import AmazingData failed: {type(e).__name__}: {e}")
        return _finish(report, out_path)

    username = os.environ.get("AMAZINGDATA_USERNAME", "")
    password = os.environ.get("AMAZINGDATA_PASSWORD", "")
    host = os.environ.get("AMAZINGDATA_HOST", "")
    port_raw = os.environ.get("AMAZINGDATA_PORT", "0")
    port = int(port_raw) if port_raw else 0
    if not all([username, password, host, port]):
        report["errors"].append("missing credentials in env")
        return _finish(report, out_path)

    try:
        ad.login(username=username, password=password, host=host, port=port)
        report["login_ok"] = True
        _log("login OK")
    except Exception as e:
        report["login_ok"] = False
        report["errors"].append(f"login failed: {type(e).__name__}: {e}")
        return _finish(report, out_path)

    try:
        base = ad.BaseData()
        _log("BaseData created")
    except Exception as e:
        report["errors"].append(f"BaseData failed: {type(e).__name__}: {e}")
        _safe_logout(ad, report)
        return _finish(report, out_path)

    types = ["EXTRA_STOCK_A", "EXTRA_INDEX_A"]
    code_list_sets = {}
    code_info_sets = {}

    report["get_code_list"] = {}
    report["get_code_info"] = {}

    # 先测 get_code_list（基准）
    for st in types:
        _log(f"calling get_code_list(security_type={st}) ...")
        result, elapsed, err = _timed(lambda st=st: base.get_code_list(security_type=st))
        if err:
            report["get_code_list"][st] = {"ok": False, "elapsed_s": round(elapsed, 3), "error": err}
            _log(f"get_code_list({st}) FAILED in {elapsed:.3f}s: {err}")
        else:
            code_list_sets[st] = set(result)
            report["get_code_list"][st] = {
                "ok": True, "elapsed_s": round(elapsed, 3), "count": len(result),
                "sample": result[:3],
            }
            _log(f"get_code_list({st}) returned {len(result)} codes in {elapsed:.3f}s")

    # 再测 get_code_info
    for st in types:
        _log(f"calling get_code_info(security_type={st}) ...")
        result, elapsed, err = _timed(lambda st=st: base.get_code_info(security_type=st))
        if err:
            report["get_code_info"][st] = {"ok": False, "elapsed_s": round(elapsed, 3), "error": err}
            _log(f"get_code_info({st}) FAILED in {elapsed:.3f}s: {err}")
        else:
            df = result
            info_codes = (set(df.index.astype(str).tolist())
                          if df is not None and len(df) > 0 else set())
            code_info_sets[st] = info_codes
            entry = {
                "ok": True, "elapsed_s": round(elapsed, 3), "count": len(info_codes),
                "shape": list(df.shape) if df is not None else None,
                "columns": list(df.columns) if df is not None else None,
                "index_sample": (list(df.index.astype(str)[:3])
                                 if df is not None and len(df) > 0 else None),
            }
            report["get_code_info"][st] = entry
            _log(f"get_code_info({st}) returned {entry['count']} rows in {elapsed:.3f}s "
                 f"cols={entry['columns']}")

    # 对比两者代码集合是否一致
    comparison = {}
    for st in types:
        cl = code_list_sets.get(st)
        ci = code_info_sets.get(st)
        if cl is not None and ci is not None:
            equal = (cl == ci)
            comparison[f"{st}_codes_equal"] = equal
            if not equal:
                comparison[f"{st}_only_in_list"] = sorted(cl - ci)[:5]
                comparison[f"{st}_only_in_info"] = sorted(ci - cl)[:5]
    report["comparison"] = comparison
    _log(f"comparison: {comparison}")

    # 速度对比结论
    speedup = {}
    for st in types:
        cl_t = report["get_code_list"].get(st, {}).get("elapsed_s")
        ci_t = report["get_code_info"].get(st, {}).get("elapsed_s")
        if cl_t and ci_t:
            speedup[st] = {
                "list_s": cl_t, "info_s": ci_t,
                "info_faster_by_s": round(cl_t - ci_t, 3),
                "info_ratio": round(ci_t / cl_t, 3) if cl_t else None,
            }
    report["speedup"] = speedup
    if speedup:
        print("==== speedup ====")
        for st, s in speedup.items():
            print(f"  {st}: list={s['list_s']}s info={s['info_s']}s "
                  f"(info faster by {s['info_faster_by_s']}s, ratio={s['info_ratio']})")

    _safe_logout(ad, report)
    _log("logout done, writing report")
    return _finish(report, out_path)


def _safe_logout(ad, report):
    try:
        username = os.environ.get("AMAZINGDATA_USERNAME", "")
        ad.logout(username=username)
        report["logout_ok"] = True
    except Exception as e:
        report["logout_ok"] = False
        report["logout_error"] = f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()
    try:
        sys.exit(probe(args.out))
    except Exception as e:
        print(f"[probe] FATAL uncaught: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.exit(2)

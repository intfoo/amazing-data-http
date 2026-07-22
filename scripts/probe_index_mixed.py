"""Probe: 验证方案 A 核心假设——单 SubscribeData 能否混合订阅股票+指数。

测试项（对应设计文档的风险点）：
1. query_snapshot 混合 code_list（股票+指数）是否被接受 + 返回结构/字段差异
   → 验证 fallback 路径混合查询可行性 + 揭示 SnapshotIndex 实际字段
2. SubscribeData.register 混合 code_list 是否注册成功（register 阶段，无需盘中）
   → 验证订阅 setup 可行性
3. （仅盘中有效）回调是否同时收到 Snapshot 与 SnapshotIndex
   → 非交易时段此项为空，不影响退出码

自包含：读 .env 注入 env（同 run.py 模式）。
退出码 0 = 两个核心测试都执行完成（结果看 report，不因结论"不支持"而判失败）。
运行：python scripts/probe_index_mixed.py [--out docs/probe-index-mixed.json] [--sub-timeout 15]
"""
import argparse
import dataclasses
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = "docs/probe-index-mixed.json"
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env_creds():
    """读 .env 注入 os.environ（替代 local.config.json，与 run.py 一致）。"""
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
    # 摘要
    qs = report.get("query_snapshot_mixed", {})
    reg = report.get("subscribe_register", {})
    qs_ok = qs.get("accepted") is True
    reg_ok = reg.get("register_succeeded") is True
    print(f"probe summary: query_snapshot_mixed={qs_ok} register_mixed={reg_ok}")
    if qs_ok and reg_ok:
        print("  → 方案 A 核心假设成立（混合 list 被接受）")
    else:
        print("  → 方案 A 需调整：见 report 详情")
    return 0  # 执行完成即 0，结论看 report


def _log(msg):
    print(f"[probe +{time.monotonic()-_T0:5.1f}s] {msg}", flush=True)


_T0 = time.monotonic()


def probe(out_path=DEFAULT_OUT, sub_timeout=15):
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
        report["errors"].append(traceback.format_exc())
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
        report["errors"].append(traceback.format_exc())
        return _finish(report, out_path)

    try:
        base = ad.BaseData()
        calendar = base.get_calendar()
        trade_day = calendar[-1] if calendar else None
        report["trade_day"] = str(trade_day)
        _log(f"calendar OK last_day={trade_day}")

        # 直接用已知有效代码，跳过慢的 get_code_list 全量拉取
        # 股票：000001.SZ 平安银行、600000.SH 浦发银行
        # 指数：000001.SH 上证指数、399001.SZ 深证成指
        stock_codes = ["000001.SZ", "600000.SH"]
        index_codes = ["000001.SH", "399001.SZ"]
        report["stock_code_count"] = len(stock_codes)
        report["index_code_count"] = len(index_codes)
        report["stock_code_samples"] = stock_codes
        report["index_code_samples"] = index_codes
        report["stock_code_count"] = len(stock_codes) if stock_codes else 0
        report["index_code_count"] = len(index_codes) if index_codes else 0
        report["stock_code_samples"] = stock_codes[:3] if stock_codes else []
        report["index_code_samples"] = index_codes[:5] if index_codes else []
        # 用前 2 只股票 + 前 2 只指数做混合测试
        mixed = (stock_codes[:2] + index_codes[:2]) if (stock_codes and index_codes) else []
        report["mixed_test_codes"] = mixed
        if not mixed:
            report["errors"].append("could not build mixed code list")
            _safe_logout(ad, report)
            return _finish(report, out_path)
    except Exception as e:
        report["errors"].append(f"base data failed: {type(e).__name__}: {e}")
        report["errors"].append(traceback.format_exc())
        _safe_logout(ad, report)
        return _finish(report, out_path)

    md = ad.MarketData(calendar)
    _log("MarketData created")

    # ===== 测试 1: query_snapshot 混合 code_list =====
    _log("TEST 1: query_snapshot mixed ...")
    qs_report = {"test_codes": mixed}
    try:
        result = md.query_snapshot(mixed, begin_date=trade_day, end_date=trade_day)
        qs_report["accepted"] = True
        qs_report["result_type"] = type(result).__name__
        # 展平 {date: {code: df}} 结构（同 gateway.query_snapshot）
        flat = {}
        if isinstance(result, dict):
            for _d, inner in result.items():
                if isinstance(inner, dict):
                    for code, df in inner.items():
                        flat[code] = df
                elif inner is not None and hasattr(inner, "columns"):
                    flat["_all"] = inner
        qs_report["codes_returned"] = list(flat.keys())
        per_code = {}
        for code, df in flat.items():
            entry = {"df_type": type(df).__name__}
            if hasattr(df, "shape"):
                entry["shape"] = list(df.shape)
            if hasattr(df, "columns"):
                entry["columns"] = list(df.columns)
            if hasattr(df, "iloc") and len(df) > 0:
                row = df.iloc[-1]  # 最后一行（最新快照）
                entry["last_row_value_types"] = {c: type(row[c]).__name__ for c in df.columns}
            per_code[code] = entry
        qs_report["per_code"] = per_code
    except Exception as e:
        qs_report["accepted"] = False
        qs_report["error"] = f"{type(e).__name__}: {e}"
        qs_report["traceback"] = traceback.format_exc()
    report["query_snapshot_mixed"] = qs_report
    _log(f"TEST 1 done: accepted={qs_report.get('accepted')}")

    # ===== 测试 2: SubscribeData.register 混合 code_list =====
    _log("TEST 2: SubscribeData register mixed ...")
    reg_report = {"test_codes": mixed, "sub_timeout_sec": sub_timeout}
    try:
        from AmazingData.utils.constant import Period
        snap_period = Period.snapshot.value
        reg_report["snapshot_period_value"] = snap_period
    except Exception as e:
        reg_report["period_error"] = f"{type(e).__name__}: {e}"
        report["subscribe_register"] = reg_report
        _safe_logout(ad, report)
        return _finish(report, out_path)

    received = {"count": 0, "by_type": {}, "sample_fields": {}, "errors": []}

    try:
        sub = ad.SubscribeData()

        @sub.register(code_list=mixed, period=snap_period)
        def _on_data(data, period):
            received["count"] += 1
            tname = type(data).__name__
            received["by_type"][tname] = received["by_type"].get(tname, 0) + 1
            # 记录前 2 个样本的字段名（不记值，避免泄露）
            if received["count"] <= 2:
                if dataclasses.is_dataclass(data):
                    received["sample_fields"][tname] = list(data.__dataclass_fields__.keys())
                elif hasattr(data, "__dict__"):
                    received["sample_fields"][tname] = list(vars(data).keys())

        reg_report["register_succeeded"] = True
        _log("register decorator applied without exception")

        # 非交易时段收不到回调；run() 是阻塞事件循环，放 daemon 线程限时跑，
        # 仅验证 run() 能启动（不期待回调，盘中才有推送）
        run_thread = threading.Thread(target=sub.run, daemon=True, name="probe-sub")
        run_thread.start()
        run_thread.join(timeout=sub_timeout)
        reg_report["ran_in_thread"] = True
        reg_report["callback_count"] = received["count"]
        reg_report["callback_by_type"] = received["by_type"]
        reg_report["sample_fields"] = received["sample_fields"]
        if received["count"] == 0:
            reg_report["note"] = "无回调推送（可能非交易时段）；register 成功即说明混合 list 被接受"
        # 尝试 stop
        stop = getattr(sub, "stop", None)
        if stop:
            try:
                stop()
            except Exception as e:
                reg_report["stop_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        reg_report["register_succeeded"] = False
        reg_report["error"] = f"{type(e).__name__}: {e}"
        reg_report["traceback"] = traceback.format_exc()

    report["subscribe_register"] = reg_report
    _log(f"TEST 2 done: register_succeeded={reg_report.get('register_succeeded')} callbacks={reg_report.get('callback_count')}")
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
    parser.add_argument("--sub-timeout", type=int, default=15)
    args = parser.parse_args()
    try:
        sys.exit(probe(args.out, args.sub_timeout))
    except Exception as e:
        print(f"[probe] FATAL uncaught: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.exit(2)

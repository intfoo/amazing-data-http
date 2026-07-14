"""AmazingData SDK 最小探测脚本。

只输出结构摘要，不输出账号、密码或完整行情数据。
机器可读报告写入 --out 指定文件（默认 docs/probe-report.json），
stdout 仅打印一行人类摘要，退出码 0 iff login_ok and query_ok。
运行方式：
    python scripts/probe_sdk.py [--out docs/probe-report.json]
"""
import inspect
import json
import os
import sys
import traceback

DEFAULT_OUT = "docs/probe-report.json"


def _finish(report, out_path):
    """写报告文件 + 打印摘要 + 返回退出码。"""
    try:
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str, ensure_ascii=False)
    except OSError as e:
        print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
        print(f"probe FAIL: cannot write report file {out_path}: {e}")
        return 1

    ok = report.get("login_ok") is True and report.get("query_ok") is True
    if ok:
        cols = report.get("df_columns")
        cols_str = str(len(cols)) if isinstance(cols, list) else "unknown"
        print(f"probe OK: login=true query=true cols={cols_str}")
    else:
        reason = (
            "import failed" if report.get("import_ok") is False
            else report.get("login_skipped")
            or report.get("login_error")
            or report.get("calendar_error")
            or report.get("marketdata_error")
            or report.get("query_error")
            or "unknown"
        )
        print(f"probe FAIL: {reason}")
    return 0 if ok else 1


def probe(out_path=DEFAULT_OUT):
    report = {"errors": []}

    try:
        import AmazingData as ad
        report["import_ok"] = True
        report["version"] = getattr(ad, "__version__", "unknown")
    except Exception as e:
        report["import_ok"] = False
        report["errors"].append(f"import failed: {type(e).__name__}: {e}")
        report["errors"].append(traceback.format_exc())
        return _finish(report, out_path)

    # 1. login 签名
    try:
        sig = inspect.signature(ad.login)
        report["login_params"] = {
            name: {"required": p.default is p.empty,
                   "default": str(p.default) if p.default is not p.empty else None}
            for name, p in sig.parameters.items()
        }
    except Exception as e:
        report["login_params"] = f"inspect failed: {e}"

    # 2. Period 枚举
    try:
        from AmazingData.utils.constant import Period
        period_names = ["day", "min1", "min3", "min5", "min10", "min15",
                        "min30", "min60", "min120", "week", "month", "season", "year"]
        report["period_values"] = {
            name: getattr(Period, name).value for name in period_names if hasattr(Period, name)
        }
        report["period_class"] = str(type(Period))
    except Exception as e:
        report["period_values"] = f"failed: {type(e).__name__}: {e}"

    # 3. 登录（需要环境变量）
    username = os.environ.get("AMAZINGDATA_USERNAME", "")
    password = os.environ.get("AMAZINGDATA_PASSWORD", "")
    host = os.environ.get("AMAZINGDATA_HOST", "")
    port_raw = os.environ.get("AMAZINGDATA_PORT", "0")
    port = int(port_raw) if port_raw else 0

    if not all([username, password, host, port]):
        report["login_skipped"] = "missing credentials in env"
        return _finish(report, out_path)

    try:
        ad.login(username=username, password=password, host=host, port=port)
        report["login_ok"] = True
    except Exception as e:
        report["login_ok"] = False
        report["login_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        return _finish(report, out_path)

    # 4. BaseData + calendar
    try:
        base = ad.BaseData()
        calendar = base.get_calendar()
        report["calendar_type"] = type(calendar).__name__
        report["calendar_length"] = len(calendar) if hasattr(calendar, "__len__") else "unknown"
        if calendar:
            report["calendar_elem_type"] = type(calendar[-1]).__name__
            report["calendar_last_sample"] = str(calendar[-1])
    except Exception as e:
        report["calendar_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        _safe_logout(ad, report)
        return _finish(report, out_path)

    # 5. MarketData
    try:
        md = ad.MarketData(calendar)
        report["marketdata_created"] = True
        sig_qk = inspect.signature(md.query_kline)
        report["query_kline_params"] = {
            name: {"required": p.default is p.empty,
                   "default": str(p.default) if p.default is not p.empty else None}
            for name, p in sig_qk.parameters.items()
        }
    except Exception as e:
        report["marketdata_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        _safe_logout(ad, report)
        return _finish(report, out_path)

    # 6. 查询一个代码一个交易日
    trade_day = calendar[-1]
    prev_day = calendar[-2] if len(calendar) > 1 else trade_day
    try:
        result = md.query_kline(
            ["000001.SZ"],
            begin_date=prev_day,
            end_date=trade_day,
            period=ad.constant.Period.day.value,
        )
        report["query_ok"] = True
        report["result_type"] = type(result).__name__

        if isinstance(result, dict):
            report["result_key_count"] = len(result)
            for code, df in result.items():
                report["result_key_sample"] = code
                report["df_type"] = type(df).__name__
                if hasattr(df, "shape"):
                    report["df_shape"] = list(df.shape)
                if hasattr(df, "columns"):
                    report["df_columns"] = list(df.columns)
                if hasattr(df, "index"):
                    report["df_index_name"] = str(df.index.name)
                    report["df_index_dtype"] = str(df.index.dtype)
                if hasattr(df, "dtypes"):
                    report["df_dtypes"] = {col: str(dt) for col, dt in df.dtypes.items()}
                if hasattr(df, "iloc") and len(df) > 0:
                    row = df.iloc[0]
                    report["df_row0_value_types"] = {col: type(row[col]).__name__ for col in df.columns}
                    report["df_row0_index_type"] = type(df.index[0]).__name__
                break
        elif hasattr(result, "columns"):
            report["result_is_dataframe"] = True
            report["df_columns"] = list(result.columns)
            report["df_index_name"] = str(result.index.name)
    except Exception as e:
        report["query_ok"] = False
        report["query_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())

    # 7. 登出
    _safe_logout(ad, report)

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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()
    sys.exit(probe(args.out))

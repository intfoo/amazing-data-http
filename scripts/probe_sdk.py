"""AmazingData SDK 最小探测脚本。

只输出结构摘要，不输出账号、密码或完整行情数据。
运行方式（在 Docker 容器内）：
    python scripts/probe_sdk.py > docs/probe-report.json
"""
import inspect
import json
import os
import sys
import traceback


def probe():
    report = {"errors": []}

    try:
        import AmazingData as ad
        report["import_ok"] = True
        report["version"] = getattr(ad, "__version__", "unknown")
    except Exception as e:
        report["import_ok"] = False
        report["errors"].append(f"import failed: {type(e).__name__}: {e}")
        report["errors"].append(traceback.format_exc())
        print(json.dumps(report, indent=2, default=str))
        return

    # 1. login 签名
    try:
        sig = inspect.signature(ad.login)
        report["login_params"] = {
            name: {"required": p.default is p.empty, "default": str(p.default) if p.default is not p.empty else None}
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
    ip = os.environ.get("AMAZINGDATA_IP", "")
    port_raw = os.environ.get("AMAZINGDATA_PORT", "0")
    port = int(port_raw) if port_raw else 0

    if not all([username, password, ip, port]):
        report["login_skipped"] = "missing credentials in env"
        print(json.dumps(report, indent=2, default=str))
        return

    try:
        ad.login(username=username, password=password, host=ip, port=port)
        report["login_ok"] = True
    except Exception as e:
        report["login_ok"] = False
        report["login_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        print(json.dumps(report, indent=2, default=str))
        return

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
        print(json.dumps(report, indent=2, default=str))
        return

    # 5. MarketData
    try:
        md = ad.MarketData(calendar)
        report["marketdata_created"] = True
        sig_qk = inspect.signature(md.query_kline)
        report["query_kline_params"] = {
            name: {"required": p.default is p.empty, "default": str(p.default) if p.default is not p.empty else None}
            for name, p in sig_qk.parameters.items()
        }
    except Exception as e:
        report["marketdata_error"] = f"{type(e).__name__}: {e}"
        report["errors"].append(traceback.format_exc())
        _safe_logout(ad, report)
        print(json.dumps(report, indent=2, default=str))
        return

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

    print(json.dumps(report, indent=2, default=str))


def _safe_logout(ad, report):
    try:
        username = os.environ.get("AMAZINGDATA_USERNAME", "")
        ad.logout(username=username)
        report["logout_ok"] = True
    except Exception as e:
        report["logout_ok"] = False
        report["logout_error"] = f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    probe()

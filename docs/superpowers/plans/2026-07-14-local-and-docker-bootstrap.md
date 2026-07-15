# 本地 & Docker 统一启动入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `scripts/run.py` 统一入口，交互式完成本地 / Docker 两种部署验证；重构 `probe_sdk.py` 输出契约修复 JSON 污染 bug；修正 `requires-python`。

**Architecture:** env 注入（零改动 app 代码）+ probe 写文件+退出码契约 + 交互向导 + docker 编排（build/compose 询问确认）。

**Tech Stack:** Python 3.13/3.14, FastAPI/uvicorn, pandas, pytest, Docker。

## Global Constraints

- Python 解释器必须是 3.13 或 3.14（SDK wheel 仅 cp313/cp314）。
- 不改 `app/*` 业务代码、`Dockerfile`、`docker-compose.yml`、`.env.example`。
- 不引入 `python-dotenv`。
- `local.config.json` 与 `.env` 互不读取。
- Windows PowerShell 为主环境；subprocess 用 list 形式，禁用 `shell=True`。
- 现有 45 个单测必须继续通过（FakeGateway 边界）。
- **Git 纪律：本计划各任务的 commit 步骤在本次无人值守执行中跳过**（遵循全局"未经用户明确要求不提交"规则）；实现完成后由用户统一 review/commit。subagent 不得执行 `git commit`。

---

## File Structure

- Create: `scripts/run.py` — 统一入口（向导 + 编排）
- Modify: `scripts/probe_sdk.py` — 输出契约重构
- Modify: `pyproject.toml` — `requires-python` 一行
- Modify: `.gitignore` — +`local.config.json`
- Modify: `README.md` — 方式二/三重写
- Create: `tests/test_probe_sdk.py` — probe 契约测试
- Create: `tests/test_run.py` — run.py 纯函数测试

依赖关系：Task 2（run.py）依赖 Task 1（probe_sdk.py）的契约（`--out` + 退出码），但契约在 spec 已固定，可并行实现。Task 3 与前两者独立。

---

## Task 1: `probe_sdk.py` 输出契约重构

**Files:**
- Modify: `scripts/probe_sdk.py`
- Test: `tests/test_probe_sdk.py`

**Interfaces:**
- Produces: `probe(out_path="docs/probe-report.json") -> int`；`_finish(report, out_path) -> int`；CLI `--out`；退出码 0 iff `login_ok and query_ok`。

- [ ] **Step 1: 写失败测试 `tests/test_probe_sdk.py`**

```python
"""probe_sdk.py 输出契约测试：用 fake AmazingData 模块，不依赖真实 SDK。"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = ROOT / "scripts" / "probe_sdk.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_sdk_under_test", PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_fake_ad(monkeypatch, *, login_raises=False, query_raises=False):
    fake = types.ModuleType("AmazingData")
    fake.__version__ = "1.1.7-test"

    def _login(**kw):
        if login_raises:
            raise RuntimeError("login boom")

    fake.login = _login
    fake.logout = lambda **kw: None

    class BaseData:
        def get_calendar(self):
            return [20240101, 20240102]

    fake.BaseData = BaseData

    class MarketData:
        def __init__(self, cal):
            self._cal = cal

        def query_kline(self, codes, **kw):
            if query_raises:
                raise RuntimeError("query boom")
            import pandas as pd
            df = pd.DataFrame({
                "code": ["000001.SZ"],
                "kline_time": [pd.Timestamp("2024-01-02")],
                "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                "volume": [100], "amount": [1000.0],
            })
            return {"000001.SZ": df}

    fake.MarketData = MarketData

    const = types.ModuleType("AmazingData.utils.constant")

    class _Day:
        value = 10008

    class Period:
        day = _Day

    const.Period = Period
    fake.constant = const

    monkeypatch.setitem(sys.modules, "AmazingData", fake)
    monkeypatch.setitem(sys.modules, "AmazingData.utils", types.ModuleType("AmazingData.utils"))
    monkeypatch.setitem(sys.modules, "AmazingData.utils.constant", const)
    for k, v in [("AMAZINGDATA_USERNAME", "u"), ("AMAZINGDATA_PASSWORD", "p"),
                 ("AMAZINGDATA_IP", "1.2.3.4"), ("AMAZINGDATA_PORT", "8600")]:
        monkeypatch.setenv(k, v)


def test_success_writes_valid_json_and_exit0(monkeypatch, tmp_path):
    _install_fake_ad(monkeypatch)
    probe = _load_probe()
    out = tmp_path / "report.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["login_ok"] is True
    assert data["query_ok"] is True
    assert "df_columns" in data


def test_login_failure_exit1(monkeypatch, tmp_path):
    _install_fake_ad(monkeypatch, login_raises=True)
    probe = _load_probe()
    out = tmp_path / "report.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["login_ok"] is False


def test_query_failure_exit1(monkeypatch, tmp_path):
    _install_fake_ad(monkeypatch, query_raises=True)
    probe = _load_probe()
    out = tmp_path / "report.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["query_ok"] is False


def test_missing_credentials_exit1(monkeypatch, tmp_path):
    # 不安装 env
    for k in ["AMAZINGDATA_USERNAME", "AMAZINGDATA_PASSWORD", "AMAZINGDATA_IP", "AMAZINGDATA_PORT"]:
        monkeypatch.delenv(k, raising=False)
    probe = _load_probe()
    out = tmp_path / "report.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 1


def test_out_override(monkeypatch, tmp_path):
    _install_fake_ad(monkeypatch)
    probe = _load_probe()
    out = tmp_path / "deep" / "nested" / "r.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 0
    assert out.exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node node_modules/jest/bin/jest.js` — 不适用，这是 Python。用：
`py -3.13 -m pytest tests/test_probe_sdk.py -v` (或 `python -m pytest`)
Expected: FAIL（`probe()` 不接受 `out_path` / 不写文件）

- [ ] **Step 3: 重构 `scripts/probe_sdk.py`**

完整新文件内容：

```python
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
    ip = os.environ.get("AMAZINGDATA_IP", "")
    port_raw = os.environ.get("AMAZINGDATA_PORT", "0")
    port = int(port_raw) if port_raw else 0

    if not all([username, password, ip, port]):
        report["login_skipped"] = "missing credentials in env"
        return _finish(report, out_path)

    try:
        ad.login(username=username, password=password, host=ip, port=port)
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_probe_sdk.py -v`
Expected: 5 passed

- [ ] **Step 5: 跑全套确认无回归**

Run: `python -m pytest -v`
Expected: 原 45 + 新 5 = 50 passed（若原套件无 probe 测试则 50）

- [ ] **Step 6: Commit（本次无人值守跳过）**

---

## Task 2: `scripts/run.py` 统一入口

**Files:**
- Create: `scripts/run.py`
- Test: `tests/test_run.py`

**Interfaces:**
- Consumes: `probe_sdk.py` 契约（`--out` + 退出码）
- Produces: `run.py` 可执行；函数见表

- [ ] **Step 1: 写失败测试 `tests/test_run.py`**

```python
"""run.py 纯函数测试，不依赖真实 SDK / docker。"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN_PATH = ROOT / "scripts" / "run.py"


def _load_run():
    spec = importlib.util.spec_from_file_location("run_under_test", RUN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_write_load_local_config_roundtrip(tmp_path):
    run = _load_run()
    p = tmp_path / "local.config.json"
    creds = {"AMAZINGDATA_USERNAME": "u", "AMAZINGDATA_PASSWORD": "p",
             "AMAZINGDATA_IP": "1.2.3.4", "AMAZINGDATA_PORT": "8600"}
    run.write_local_config(p, creds)
    data = run.load_local_config(p)
    assert data["AMAZINGDATA_USERNAME"] == "u"
    assert data["HTTP_HOST"] == "0.0.0.0"
    assert data["HTTP_PORT"] == "3021"


def test_load_local_config_corrupt_json_raises(tmp_path):
    run = _load_run()
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        run.load_local_config(p)


def test_write_env_file_format(tmp_path):
    run = _load_run()
    p = tmp_path / ".env"
    creds = {"AMAZINGDATA_USERNAME": "u", "AMAZINGDATA_PASSWORD": "p",
             "AMAZINGDATA_IP": "1.2.3.4", "AMAZINGDATA_PORT": "8600"}
    run.write_env_file(p, creds)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "AMAZINGDATA_USERNAME=u"
    assert lines[3] == "AMAZINGDATA_PORT=8600"
    assert lines[4] == "HTTP_HOST=0.0.0.0"
    assert lines[5] == "HTTP_PORT=3021"


def test_build_docker_build_cmd_has_tag():
    run = _load_run()
    cmd = run.build_docker_build_cmd()
    assert "-t" in cmd and "amazingdata-http:probe" in cmd


def test_build_docker_probe_cmd_has_out_and_forward_slash(tmp_path):
    run = _load_run()
    docs = tmp_path / "docs"
    cmd = run.build_docker_probe_cmd(docs)
    assert "--out" in cmd and "docs/probe-report.json" in cmd
    vol = cmd[cmd.index("-v") + 1]
    assert "\\" not in vol


def test_build_docker_compose_cmd():
    run = _load_run()
    assert run.build_docker_compose_cmd() == ["docker", "compose", "up", "-d"]


def test_pick_sdk_wheels_313(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    wheels = run.pick_sdk_wheels()
    assert any("cp313" in w for w in wheels)
    assert any("tgw" in w for w in wheels)


def test_pick_sdk_wheels_314(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    wheels = run.pick_sdk_wheels()
    assert any("cp314" in w for w in wheels)


def test_pick_sdk_wheels_unsupported_raises(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 12, 0, "final", 0))
    with pytest.raises(ValueError):
        run.pick_sdk_wheels()


def test_check_python_version_exits_on_unsupported(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 12, 0, "final", 0))
    with pytest.raises(SystemExit):
        run.check_python_version()


def test_inject_env_sets_and_cleanup(monkeypatch):
    run = _load_run()
    for k in ["AMAZINGDATA_USERNAME", "HTTP_PORT"]:
        monkeypatch.delenv(k, raising=False)
    run.inject_env({"AMAZINGDATA_USERNAME": "u", "HTTP_PORT": "3021"})
    assert os.environ["AMAZINGDATA_USERNAME"] == "u"
    assert os.environ["HTTP_PORT"] == "3021"


def test_run_probe_uses_list_form_and_returns_code(monkeypatch):
    run = _load_run()
    captured = {}

    class FakeProc:
        returncode = 0

    def fake_runner(cmd, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return FakeProc()

    rc = run.run_probe("docs/probe-report.json", runner=fake_runner)
    assert rc == 0
    assert isinstance(captured["cmd"], list)
    assert "--out" in captured["cmd"]
    assert "docs/probe-report.json" in captured["cmd"]


def test_ask_credentials_validates_empty_and_port():
    run = _load_run()
    inputs = iter(["", "user", "1.2.3.4", "abc", "8600"])
    passes = iter(["pw"])

    def fake_input(prompt):
        return next(inputs)

    def fake_getpass(prompt):
        return next(passes)

    creds = run.ask_credentials(input_fn=fake_input, getpass_fn=fake_getpass)
    assert creds["AMAZINGDATA_USERNAME"] == "user"
    assert creds["AMAZINGDATA_PORT"] == "8600"


def test_confirm_yes_variants_and_default_no():
    run = _load_run()
    assert run.confirm("ok", input_fn=lambda p: "y") is True
    assert run.confirm("ok", input_fn=lambda p: "YES") is True
    assert run.confirm("ok", input_fn=lambda p: "") is False
    assert run.confirm("ok", input_fn=lambda p: "n") is False


def test_backup_env_copies(tmp_path):
    run = _load_run()
    p = tmp_path / ".env"
    p.write_text("OLD=1", encoding="utf-8")
    run.backup_env(p)
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "OLD=1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_run.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 创建 `scripts/run.py`**

完整文件内容：

```python
"""统一本地 & Docker 启动入口。

交互式向导收集凭据 → 可选装 SDK → probe 门禁 → 本地起 uvicorn 或编排 docker。
本地模式凭据写 local.config.json（注入 os.environ，零改动 app 代码）；
Docker 模式凭据写 .env。二者互不读取。
"""
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = PROJECT_ROOT / "local.config.json"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
PROBE_REPORT = PROJECT_ROOT / "docs" / "probe-report.json"
PROBE_SCRIPT = PROJECT_ROOT / "scripts" / "probe_sdk.py"

SUPPORTED_PY = {(3, 13), (3, 14)}
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = "3021"


def check_python_version():
    if sys.version_info[:2] not in SUPPORTED_PY:
        print(f"需要 Python 3.13 或 3.14，当前 {sys.version_info[0]}.{sys.version_info[1]}")
        sys.exit(1)


def pick_sdk_wheels():
    ver = sys.version_info[:2]
    if ver == (3, 13):
        ad_tag = "cp313"
    elif ver == (3, 14):
        ad_tag = "cp314"
    else:
        raise ValueError(f"unsupported python {ver}")
    tgw = list(PROJECT_ROOT.glob("tgw-*-py3-none-any.whl"))
    ad = list(PROJECT_ROOT.glob(f"AmazingData-*-{ad_tag}-none-any.whl"))
    if len(tgw) != 1:
        raise FileNotFoundError(f"期望恰好 1 个 tgw wheel，找到 {[p.name for p in tgw]}")
    if len(ad) != 1:
        raise FileNotFoundError(f"期望恰好 1 个 {ad_tag} AmazingData wheel，找到 {[p.name for p in ad]}")
    return [str(tgw[0]), str(ad[0])]


def load_local_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_local_config(path, creds):
    data = {
        "AMAZINGDATA_USERNAME": creds["AMAZINGDATA_USERNAME"],
        "AMAZINGDATA_PASSWORD": creds["AMAZINGDATA_PASSWORD"],
        "AMAZINGDATA_IP": creds["AMAZINGDATA_IP"],
        "AMAZINGDATA_PORT": creds["AMAZINGDATA_PORT"],
        "HTTP_HOST": DEFAULT_HTTP_HOST,
        "HTTP_PORT": DEFAULT_HTTP_PORT,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_env_file(path, creds):
    lines = [
        f"AMAZINGDATA_USERNAME={creds['AMAZINGDATA_USERNAME']}",
        f"AMAZINGDATA_PASSWORD={creds['AMAZINGDATA_PASSWORD']}",
        f"AMAZINGDATA_IP={creds['AMAZINGDATA_IP']}",
        f"AMAZINGDATA_PORT={creds['AMAZINGDATA_PORT']}",
        f"HTTP_HOST={DEFAULT_HTTP_HOST}",
        f"HTTP_PORT={DEFAULT_HTTP_PORT}",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def inject_env(config):
    for k, v in config.items():
        os.environ[k] = str(v)


def build_docker_build_cmd():
    return ["docker", "build", "--platform", "linux/amd64", "-t", "amazingdata-http:probe", "."]


def build_docker_probe_cmd(docs_abs):
    docs_vol = str(Path(docs_abs).resolve()).replace("\\", "/")
    return ["docker", "run", "--rm", "--env-file", ".env", "--platform", "linux/amd64",
            "-v", f"{docs_vol}:/app/docs", "amazingdata-http:probe",
            "python", "scripts/probe_sdk.py", "--out", "docs/probe-report.json"]


def build_docker_compose_cmd():
    return ["docker", "compose", "up", "-d"]


def backup_env(path):
    p = Path(path)
    if p.exists():
        shutil.copyfile(p, str(p) + ".bak")


def run_probe(out_path, runner=subprocess.run):
    cmd = [sys.executable, str(PROBE_SCRIPT), "--out", str(out_path)]
    proc = runner(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode if hasattr(proc, "returncode") else proc


def confirm(prompt, input_fn=input):
    ans = input_fn(prompt + " (y/n) ").strip().lower()
    return ans in ("y", "yes")


def ask_credentials(input_fn=input, getpass_fn=getpass.getpass):
    def _ask(label):
        while True:
            v = input_fn(label + ": ").strip()
            if v:
                return v
            print("不能为空，请重新输入")

    username = _ask("AMAZINGDATA_USERNAME")
    ip = _ask("AMAZINGDATA_IP")
    while True:
        port = input_fn("AMAZINGDATA_PORT: ").strip()
        try:
            int(port)
            break
        except ValueError:
            print("端口须为整数，请重新输入")
    password = getpass_fn("AMAZINGDATA_PASSWORD: ").strip()
    while not password:
        print("不能为空，请重新输入")
        password = getpass_fn("AMAZINGDATA_PASSWORD: ").strip()
    return {
        "AMAZINGDATA_USERNAME": username,
        "AMAZINGDATA_IP": ip,
        "AMAZINGDATA_PORT": port,
        "AMAZINGDATA_PASSWORD": password,
    }


def ensure_sdk(install_fn, confirm_fn):
    try:
        import AmazingData  # noqa: F401
        return
    except ImportError:
        pass
    if confirm_fn("SDK 未安装，是否自动安装？"):
        pkgs = pick_sdk_wheels() + ["fastapi", "uvicorn[standard]", "pandas", "numpy"]
        install_fn(pkgs)
    else:
        print("已跳过安装。请手动执行：")
        manual = " ".join(pick_sdk_wheels() + ["fastapi", "uvicorn[standard]", "pandas", "numpy"])
        print(f"  {sys.executable} -m pip install {manual}")
        sys.exit(1)


def _pip_install(pkgs):
    subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], check=False)


def docker_available():
    return shutil.which("docker") is not None


def _rc(proc):
    return proc.returncode if hasattr(proc, "returncode") else proc


def run_local(input_fn=input, getpass_fn=getpass.getpass, confirm_fn=confirm,
              install_fn=_pip_install, runner=subprocess.run):
    config_ok = False
    creds = None
    if LOCAL_CONFIG.exists():
        try:
            creds = load_local_config(LOCAL_CONFIG)
            config_ok = True
        except (json.JSONDecodeError, OSError):
            print("local.config.json 损坏或不可读，进入向导重新配置")
    if not config_ok:
        for _ in range(3):
            creds = ask_credentials(input_fn, getpass_fn)
            ensure_sdk(install_fn, confirm_fn)
            inject_env(creds)
            rc = run_probe(str(PROBE_REPORT), runner=runner)
            if rc == 0:
                break
            print("凭据或网络有问题，请重试")
        else:
            print("连续 3 次验证失败，退出。")
            sys.exit(1)
        write_local_config(LOCAL_CONFIG, creds)
    else:
        ensure_sdk(install_fn, confirm_fn)
        inject_env(creds)
        rc = run_probe(str(PROBE_REPORT), runner=runner)
        if rc != 0:
            print("probe 门禁失败，请检查凭据/网络后重跑。")
            sys.exit(1)

    import uvicorn
    uvicorn.run("app.http_app:app",
                host=creds.get("HTTP_HOST", DEFAULT_HTTP_HOST),
                port=int(creds.get("HTTP_PORT", DEFAULT_HTTP_PORT)))


def run_docker(input_fn=input, getpass_fn=getpass.getpass, confirm_fn=confirm,
               runner=subprocess.run):
    if not docker_available():
        print("docker 未安装或未运行，请先安装 Docker Desktop。")
        sys.exit(1)
    creds = ask_credentials(input_fn, getpass_fn)
    if ENV_FILE.exists():
        if confirm_fn(".env 已存在，覆盖？(会备份为 .env.bak)"):
            backup_env(ENV_FILE)
            write_env_file(ENV_FILE, creds)
    else:
        write_env_file(ENV_FILE, creds)

    if confirm_fn("执行 docker build？"):
        if _rc(runner(build_docker_build_cmd(), cwd=str(PROJECT_ROOT))) != 0:
            print("docker build 失败")
            sys.exit(1)
        if _rc(runner(build_docker_probe_cmd(PROJECT_ROOT / "docs"), cwd=str(PROJECT_ROOT))) != 0:
            print("probe 门禁失败：凭据可能有误，.env 已更新，建议修正后重跑")
    else:
        print("跳过 build，手动执行：")
        print(" ".join(build_docker_build_cmd()))

    if confirm_fn("执行 docker compose up -d？"):
        runner(build_docker_compose_cmd(), cwd=str(PROJECT_ROOT))
    else:
        print("跳过 compose，手动执行：")
        print(" ".join(build_docker_compose_cmd()))

    print("手动验证：")
    print("  curl http://localhost:3021/health")
    print("  若返回 503：docker compose logs amazingdata-http 查登录错误")


def main():
    check_python_version()
    print(f"Python {sys.version}")
    print("选择模式：1=本地真实 SDK  2=Docker 完整链路")
    choice = input("模式 [1/2]: ").strip()
    if choice == "1":
        run_local()
    elif choice == "2":
        run_docker()
    else:
        print("无效选择")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_run.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit（本次无人值守跳过）**

---

## Task 3: 配置 & 文档

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Modify: `README.md`

- [ ] **Step 1: 改 `pyproject.toml`**

把 `requires-python = ">=3.14"` 改为 `requires-python = ">=3.13"`。

- [ ] **Step 2: 改 `.gitignore`**

在 `.env` 行下方加 `local.config.json`：

```
.env
local.config.json
```

- [ ] **Step 3: 改 `README.md` 方式二**

把"## 验证方式二：本地真实 SDK..."整节（从标题到"## 验证方式三"之前）替换为：

```markdown
## 验证方式二：本地真实 SDK（需要 Python 3.13 或 3.14 + 凭据，无需 Docker）

一条命令完成：交互式填凭据 →（可选）装 SDK → probe 登录验证 → 起 uvicorn。

```bash
python scripts/run.py
# 选模式 1=本地
```

首次运行会逐项询问用户名 / IP / 端口 / 密码（密码隐藏输入），probe 登录验证通过后写入 `local.config.json`（gitignored），随后每次启动自动读取并重跑 probe 门禁。

> SDK wheel 按 Python 解释器版本自动选 `cp313` / `cp314`；`import AmazingData` 失败时会询问是否自动 `pip install`。

启动后用以下命令手动验证：

```bash
curl http://localhost:3021/health
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```

> 本地模式凭据存 `local.config.json`，Docker 模式存 `.env`，二者互不读取。
```

- [ ] **Step 4: 改 `README.md` 方式三**

把"## 验证方式三：Docker 完整链路..."整节（从标题到"### 主项目集成配置"之前）替换为：

```markdown
## 验证方式三：Docker 完整链路（需要 Docker + 凭据）

```bash
python scripts/run.py
# 选模式 2=Docker
```

交互式填凭据后自动生成 `.env`（若已存在会询问覆盖，旧文件备份为 `.env.bak`）；随后询问是否执行 `docker build`、容器内 probe 门禁、`docker compose up -d`，每步可选 n 改为手动执行。

启动后手动验证：

```bash
curl http://localhost:3021/health          # 期望 {"status":"ok"}
# 若 503：docker compose logs amazingdata-http 查登录错误
curl -X POST http://localhost:3021/daily -H "Content-Type: application/json" -d "{\"symbols\":[\"000001.SZ\"],\"start_time\":\"2024-01-02\",\"end_time\":\"2024-01-31\"}"
```

错误场景验证（反向日期 / 空代码 / 错误格式）同 API 参考章节。
```

- [ ] **Step 5: 改 `README.md` 前置条件**

把前置条件里 Python 版本相关描述确认与"3.13 或 3.14"一致（已是，无需改）；环境变量章节末尾补一句：

```markdown
> 本地模式用 `local.config.json`，Docker 模式用 `.env`，二者互不读取。
```

- [ ] **Step 6: 跑全套确认无回归**

Run: `python -m pytest -v`
Expected: 全绿（原 45 + Task1 的 5 + Task2 的 14 = 64）

- [ ] **Step 7: Commit（本次无人值守跳过）**

---

## Self-Review

- **Spec 覆盖**：§4.3 probe 契约 → Task 1；§4.4 run.py 函数 → Task 2（全部函数均有测试）；§4.1 pyproject/gitignore/README → Task 3；§5 流程 → run_local/run_docker。✓
- **占位符**：无 TBD/TODO，每步含完整代码。✓
- **类型一致**：`run_probe` 返回 int、`ask_credentials` 返回 dict、`confirm` 返回 bool，跨任务一致。✓
- **风险**：Task 1 重构保留全部探测逻辑；Task 2 env 注入 + 字符串路径 uvicorn（§4.2 约束在 run_local 注释体现）；Task 3 仅文本+1 行配置。✓

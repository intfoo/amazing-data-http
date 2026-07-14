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
                 ("AMAZINGDATA_HOST", "1.2.3.4"), ("AMAZINGDATA_PORT", "8600")]:
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
    # 安装 fake SDK 但不设环境变量 → 走 login_skipped 路径（环境无关）
    _install_fake_ad(monkeypatch)
    for k in ["AMAZINGDATA_USERNAME", "AMAZINGDATA_PASSWORD", "AMAZINGDATA_HOST", "AMAZINGDATA_PORT"]:
        monkeypatch.delenv(k, raising=False)
    probe = _load_probe()
    out = tmp_path / "report.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["login_skipped"] == "missing credentials in env"


def test_out_override(monkeypatch, tmp_path):
    _install_fake_ad(monkeypatch)
    probe = _load_probe()
    out = tmp_path / "deep" / "nested" / "r.json"
    rc = probe.probe(out_path=str(out))
    assert rc == 0
    assert out.exists()

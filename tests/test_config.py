import os
from app.config import Config


def test_config_from_env_reads_all_vars():
    os.environ["AMAZINGDATA_USERNAME"] = "user1"
    os.environ["AMAZINGDATA_PASSWORD"] = "pass1"
    os.environ["AMAZINGDATA_HOST"] = "1.2.3.4"
    os.environ["AMAZINGDATA_PORT"] = "3021"
    os.environ["HTTP_HOST"] = "0.0.0.0"
    os.environ["HTTP_PORT"] = "8080"
    cfg = Config.from_env()
    assert cfg.username == "user1"
    assert cfg.password == "pass1"
    assert cfg.ip == "1.2.3.4"
    assert cfg.port == 3021
    assert cfg.http_host == "0.0.0.0"
    assert cfg.http_port == 8080
    assert cfg.is_configured() is True


def test_config_defaults():
    for key in ["AMAZINGDATA_USERNAME", "AMAZINGDATA_PASSWORD", "AMAZINGDATA_HOST", "AMAZINGDATA_PORT"]:
        os.environ.pop(key, None)
    os.environ["HTTP_HOST"] = ""
    os.environ["HTTP_PORT"] = ""
    cfg = Config.from_env()
    assert cfg.http_host == "0.0.0.0"
    assert cfg.http_port == 3021
    assert cfg.is_configured() is False


def test_config_reads_sdk_max_concurrent(monkeypatch):
    monkeypatch.setenv("SDK_MAX_CONCURRENT", "3")
    cfg = Config.from_env()
    assert cfg.sdk_max_concurrent == 3


def test_config_default_sdk_max_concurrent(monkeypatch):
    monkeypatch.delenv("SDK_MAX_CONCURRENT", raising=False)
    cfg = Config.from_env()
    assert cfg.sdk_max_concurrent == 2


def test_config_default_adj_factor_local_path(monkeypatch):
    monkeypatch.delenv("ADJ_FACTOR_LOCAL_PATH", raising=False)
    cfg = Config.from_env()
    assert cfg.adj_factor_local_path == ""


def test_config_reads_adj_factor_local_path(monkeypatch):
    monkeypatch.setenv("ADJ_FACTOR_LOCAL_PATH", "D://cache//adj//")
    cfg = Config.from_env()
    assert cfg.adj_factor_local_path == "D://cache//adj//"


def test_config_default_subscription_window(monkeypatch):
    monkeypatch.delenv("SUBSCRIPTION_OPEN", raising=False)
    monkeypatch.delenv("SUBSCRIPTION_CLOSE", raising=False)
    cfg = Config.from_env()
    assert cfg.subscription_open == "09:00"
    assert cfg.subscription_close == "15:20"


def test_config_reads_subscription_window(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_OPEN", "08:55")
    monkeypatch.setenv("SUBSCRIPTION_CLOSE", "15:30")
    cfg = Config.from_env()
    assert cfg.subscription_open == "08:55"
    assert cfg.subscription_close == "15:30"


def test_config_default_stale_threshold(monkeypatch):
    monkeypatch.delenv("STALE_THRESHOLD_SEC", raising=False)
    cfg = Config.from_env()
    assert cfg.stale_threshold_sec == 90


def test_config_reads_stale_threshold(monkeypatch):
    monkeypatch.setenv("STALE_THRESHOLD_SEC", "120")
    cfg = Config.from_env()
    assert cfg.stale_threshold_sec == 120


def test_config_default_watchdog_interval(monkeypatch):
    monkeypatch.delenv("WATCHDOG_INTERVAL_SEC", raising=False)
    cfg = Config.from_env()
    assert cfg.watchdog_interval_sec == 60


def test_config_reads_watchdog_interval(monkeypatch):
    monkeypatch.setenv("WATCHDOG_INTERVAL_SEC", "30")
    cfg = Config.from_env()
    assert cfg.watchdog_interval_sec == 30


def test_config_default_calendar_fallback_weekday(monkeypatch):
    monkeypatch.delenv("CALENDAR_FALLBACK_WEEKDAY", raising=False)
    cfg = Config.from_env()
    assert cfg.calendar_fallback_weekday is True


def test_config_reads_calendar_fallback_weekday(monkeypatch):
    monkeypatch.setenv("CALENDAR_FALLBACK_WEEKDAY", "false")
    cfg = Config.from_env()
    assert cfg.calendar_fallback_weekday is False


def test_config_malformed_int_env_falls_back(monkeypatch):
    """畸形 int 环境变量不崩溃，warning 后回退默认值。"""
    monkeypatch.setenv("AMAZINGDATA_PORT", "not-a-number")
    monkeypatch.setenv("SDK_MAX_CONCURRENT", "abc")
    cfg = Config.from_env()
    assert cfg.port == 0
    assert cfg.sdk_max_concurrent == 2


def test_config_etf_flow_cache_ttl(monkeypatch):
    monkeypatch.delenv("ETF_FLOW_CACHE_TTL_SEC", raising=False)
    assert Config.from_env().etf_flow_cache_ttl_sec == 300
    monkeypatch.setenv("ETF_FLOW_CACHE_TTL_SEC", "600")
    assert Config.from_env().etf_flow_cache_ttl_sec == 600

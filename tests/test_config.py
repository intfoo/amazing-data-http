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
    assert cfg.sdk_max_concurrent == 5

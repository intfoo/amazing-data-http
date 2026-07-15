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
             "AMAZINGDATA_HOST": "1.2.3.4", "AMAZINGDATA_PORT": "8600"}
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
             "AMAZINGDATA_HOST": "1.2.3.4", "AMAZINGDATA_PORT": "8600"}
    run.write_env_file(p, creds)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "AMAZINGDATA_USERNAME=u"
    assert lines[2] == "AMAZINGDATA_HOST=1.2.3.4"
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

    def fake_runner(cmd, cwd=None, **kwargs):
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


def test_ask_credentials_password_empty_retry():
    run = _load_run()
    inputs = iter(["user", "1.2.3.4", "8600"])
    passes = iter(["", "pw"])
    creds = run.ask_credentials(input_fn=lambda p: next(inputs),
                                getpass_fn=lambda p: next(passes))
    assert creds["AMAZINGDATA_PASSWORD"] == "pw"


def test_summarize_config_hides_password(capsys):
    run = _load_run()
    run.summarize_config({
        "AMAZINGDATA_USERNAME": "user1",
        "AMAZINGDATA_HOST": "1.2.3.4",
        "AMAZINGDATA_PORT": "8600",
        "HTTP_HOST": "0.0.0.0",
        "HTTP_PORT": "3021",
        "AMAZINGDATA_PASSWORD": "supersecret",
    })
    out = capsys.readouterr().out
    assert "user1" in out
    assert "1.2.3.4" in out
    assert "8600" in out
    assert "supersecret" not in out
    assert "已隐藏" in out


def test_run_local_exits_after_3_probe_failures(monkeypatch, tmp_path):
    run = _load_run()
    monkeypatch.setattr(run, "LOCAL_CONFIG", tmp_path / "local.config.json")
    monkeypatch.setattr(run, "PROBE_REPORT", tmp_path / "probe.json")
    monkeypatch.setattr(run, "ensure_sdk", lambda *a, **k: None)

    def fake_input(prompt):
        if "USERNAME" in prompt:
            return "user"
        if "HOST" in prompt:
            return "1.2.3.4"
        return "8600"  # PORT

    def fake_getpass(prompt):
        return "pw"

    class P:
        returncode = 1
    calls = {"n": 0}

    def fake_runner(cmd, cwd=None, **kwargs):
        calls["n"] += 1
        return P()

    with pytest.raises(SystemExit):
        run.run_local(input_fn=fake_input,
                      getpass_fn=fake_getpass,
                      confirm_fn=lambda p: True,
                      install_fn=lambda pkgs: None,
                      runner=fake_runner)
    assert calls["n"] == 3
    assert not (tmp_path / "local.config.json").exists()


def test_run_local_success_writes_config_and_calls_uvicorn(monkeypatch, tmp_path):
    import types as _types
    run = _load_run()
    monkeypatch.setattr(run, "LOCAL_CONFIG", tmp_path / "local.config.json")
    monkeypatch.setattr(run, "PROBE_REPORT", tmp_path / "probe.json")
    monkeypatch.setattr(run, "ensure_sdk", lambda *a, **k: None)
    inputs = iter(["user", "1.2.3.4", "8600"])
    passes = iter(["pw"])

    class P:
        returncode = 0

    def fake_runner(cmd, cwd=None, **kwargs):
        return P()

    fake_uvicorn = _types.ModuleType("uvicorn")
    called = {"run": False, "app_str": None}

    class _FakeConfig:
        def __init__(self, app_str, host=None, port=None):
            called["app_str"] = app_str

    class _FakeServer:
        def __init__(self, config):
            pass

        def run(self):
            called["run"] = True

    fake_uvicorn.Config = _FakeConfig
    fake_uvicorn.Server = _FakeServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    run.run_local(input_fn=lambda p: next(inputs),
                  getpass_fn=lambda p: next(passes),
                  confirm_fn=lambda p: True,
                  install_fn=lambda pkgs: None,
                  runner=fake_runner)
    assert called["run"] is True
    assert called["app_str"] == "app.http_app:app"
    assert (tmp_path / "local.config.json").exists()


def test_run_docker_build_fail_exits(monkeypatch, tmp_path):
    run = _load_run()
    monkeypatch.setattr(run, "docker_available", lambda: True)
    monkeypatch.setattr(run, "ENV_FILE", tmp_path / ".env")
    inputs = iter(["user", "1.2.3.4", "8600"])
    passes = iter(["pw"])

    class P1:
        returncode = 1

    def fake_runner(cmd, cwd=None, **kwargs):
        return P1()

    with pytest.raises(SystemExit):
        run.run_docker(input_fn=lambda p: next(inputs),
                       getpass_fn=lambda p: next(passes),
                       confirm_fn=lambda p: True,
                       runner=fake_runner)


def test_run_docker_probe_fail_continues_to_compose(monkeypatch, tmp_path):
    run = _load_run()
    monkeypatch.setattr(run, "docker_available", lambda: True)
    monkeypatch.setattr(run, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(run, "PROJECT_ROOT", tmp_path)
    inputs = iter(["user", "1.2.3.4", "8600"])
    passes = iter(["pw"])
    seq = iter([0, 1, 0])

    class P:
        def __init__(self, rc):
            self.returncode = rc

    def fake_runner(cmd, cwd=None, **kwargs):
        return P(next(seq))

    run.run_docker(input_fn=lambda p: next(inputs),
                   getpass_fn=lambda p: next(passes),
                   confirm_fn=lambda p: True,
                   runner=fake_runner)
    assert next(seq, "done") == "done"


def test_get_installed_sdk_versions_returns_dict():
    run = _load_run()
    versions = run.get_installed_sdk_versions()
    assert "tgw" in versions
    assert "AmazingData" in versions
    # 值为 None 或字符串版本号
    for v in versions.values():
        assert v is None or isinstance(v, str)


def test_run_install_sdk_no_wheel_exits(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "final", 0))
    # pick_sdk_wheels 对不支持的 Python 版本 raise ValueError
    with pytest.raises(SystemExit):
        run.run_install_sdk(confirm_fn=lambda p: True)


def test_run_install_sdk_cancel_exits(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    # 用户选 n → SystemExit(0)
    with pytest.raises(SystemExit) as exc_info:
        run.run_install_sdk(confirm_fn=lambda p: False)
    assert exc_info.value.code == 0


def test_run_install_sdk_success(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    captured = []

    class P:
        returncode = 0

    def fake_runner(cmd, cwd=None, **kwargs):
        captured.append(cmd)
        return P()

    # 模拟版本从 None → "1.1.9"
    version_seq = iter([
        {"tgw": None, "AmazingData": None},
        {"tgw": "1.0.9.1", "AmazingData": "1.1.9"},
    ])
    monkeypatch.setattr(run, "get_installed_sdk_versions", lambda: next(version_seq))

    run.run_install_sdk(
        runner=fake_runner,
        confirm_fn=lambda p: True,
    )
    # 应执行两步：先 --no-deps --force-reinstall，再普通 install 补依赖
    assert len(captured) == 2
    assert "--no-deps" in captured[0]
    assert "--force-reinstall" in captured[0]
    assert "--no-deps" not in captured[1]
    assert "--force-reinstall" not in captured[1]
    assert captured[0][0] == sys.executable


def test_run_install_sdk_pip_fail_exits(monkeypatch):
    run = _load_run()
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))

    class P:
        returncode = 1

    def fake_runner(cmd, cwd=None, **kwargs):
        return P()

    monkeypatch.setattr(run, "get_installed_sdk_versions",
                        lambda: {"tgw": None, "AmazingData": None})
    with pytest.raises(SystemExit):
        run.run_install_sdk(
            runner=fake_runner,
            confirm_fn=lambda p: True,
        )

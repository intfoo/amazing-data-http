"""统一本地 & Docker/Podman 启动入口。

交互式向导收集凭据 → 可选装 SDK → probe 门禁 → 本地起 uvicorn 或编排容器。
凭据统一写 .env（local 模式与 docker 模式共用），注入 os.environ，零改动 app 代码。
扩展配置（ADJ_FACTOR_LOCAL_PATH、ADJ_FACTOR_IS_LOCAL、SUBSCRIPTION_* 等）也在 .env 里。
"""
import getpass
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 脚本直接运行时 sys.path[0] 是 scripts/，不含项目根，导致 uvicorn import app.http_app 失败
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ENV_FILE = PROJECT_ROOT / ".env"
PROBE_REPORT = PROJECT_ROOT / "docs" / "probe-report.json"
PROBE_SCRIPT = PROJECT_ROOT / "scripts" / "probe_sdk.py"

SUPPORTED_PY = {(3, 13), (3, 14)}
# 本地模式（scripts/run.py 模式 1）下生效，传给 uvicorn 监听地址。
# Docker/Podman 模式下无效——Dockerfile CMD 写死了 0.0.0.0:3021，未读 .env 的 HTTP_HOST/HTTP_PORT。
# 如需容器模式支持自定义端口，需同步修改 Dockerfile CMD 与 docker-compose.yml 的 ports。
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


def write_env_file(path, creds):
    """写 .env：合并模式。保留现有 .env 的注释和扩展配置，只更新凭据键 + HTTP 配置。

    避免向导覆盖 .env 时丢失 AUTH_TOKEN、ADJ_FACTOR_IS_LOCAL、SUBSCRIPTION_* 等扩展配置。
    .env 不存在时（首次运行）只写 6 个基本键，用户需手动配置 AUTH_TOKEN 等。
    """
    cred_updates = {
        "AMAZINGDATA_USERNAME": creds["AMAZINGDATA_USERNAME"],
        "AMAZINGDATA_PASSWORD": creds["AMAZINGDATA_PASSWORD"],
        "AMAZINGDATA_HOST": creds["AMAZINGDATA_HOST"],
        "AMAZINGDATA_PORT": creds["AMAZINGDATA_PORT"],
        "HTTP_HOST": DEFAULT_HTTP_HOST,
        "HTTP_PORT": DEFAULT_HTTP_PORT,
    }
    p = Path(path)
    existing_lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    updated_keys = set()
    new_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.partition("=")[0].strip()
            if k in cred_updates:
                new_lines.append(f"{k}={cred_updates[k]}")
                updated_keys.add(k)
                continue
        new_lines.append(line)
    # 追加 .env 中不存在的凭据键
    for k, v in cred_updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")
    if not new_lines or new_lines[-1] != "":
        new_lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))


ENV_REQUIRED_KEYS = ("AMAZINGDATA_USERNAME", "AMAZINGDATA_PASSWORD",
                     "AMAZINGDATA_HOST", "AMAZINGDATA_PORT")


def load_env_file(path):
    """解析 .env 文件为字典。格式 KEY=VALUE，忽略空行和 # 注释。"""
    creds = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip()
    return creds


def env_credentials_complete(creds):
    """检查字典是否包含全部必填凭据字段且非空。"""
    return all(creds.get(k) for k in ENV_REQUIRED_KEYS)


def inject_env(config):
    for k, v in config.items():
        os.environ[k] = str(v)


def summarize_config(creds, source=".env"):
    """打印已读取的配置摘要（密码隐藏）。"""
    print(f"已读取 {source}：")
    print(f"  账号   AMAZINGDATA_USERNAME = {creds.get('AMAZINGDATA_USERNAME', '<空>')}")
    print(f"  服务器 AMAZINGDATA_HOST     = {creds.get('AMAZINGDATA_HOST', '<空>')}:{creds.get('AMAZINGDATA_PORT', '<空>')}")
    print(f"  HTTP   监听 = {creds.get('HTTP_HOST', DEFAULT_HTTP_HOST)}:{creds.get('HTTP_PORT', DEFAULT_HTTP_PORT)}")
    print("  密码   AMAZINGDATA_PASSWORD = <已隐藏>")


def build_container_build_cmd(engine="docker"):
    return [engine, "build", "--platform", "linux/amd64", "-t", "amazingdata-http:probe", "."]


def build_container_probe_cmd(docs_abs, engine="docker"):
    docs_vol = str(Path(docs_abs).resolve()).replace("\\", "/")
    return [engine, "run", "--rm", "--env-file", ".env", "--platform", "linux/amd64",
            "-v", f"{docs_vol}:/app/docs", "amazingdata-http:probe",
            "python", "scripts/probe_sdk.py", "--out", "docs/probe-report.json"]


def build_container_compose_cmd(engine="docker"):
    return [engine, "compose", "up", "-d"]


def backup_env(path):
    p = Path(path)
    if p.exists():
        shutil.copyfile(p, str(p) + ".bak")


def run_probe(out_path, runner=subprocess.run):
    cmd = [sys.executable, str(PROBE_SCRIPT), "--out", str(out_path)]
    proc = runner(cmd, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    rc = _rc(proc)
    out = ""
    if hasattr(proc, "stdout") and proc.stdout:
        out = proc.stdout.decode("utf-8", errors="replace").strip()
    if rc != 0:
        if out:
            print("---- probe 输出 ----")
            print(out)
            print("--------------------")
    else:
        # 成功只回显摘要行（屏蔽 SDK 的 TGW 登录噪声与 token）
        if out:
            print(out.splitlines()[-1])
    return rc


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
    host = _ask("AMAZINGDATA_HOST")
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
        "AMAZINGDATA_HOST": host,
        "AMAZINGDATA_PORT": port,
        "AMAZINGDATA_PASSWORD": password,
    }


def _collect_credentials(input_fn=input, getpass_fn=getpass.getpass, confirm_fn=confirm):
    """收集凭据：.env 存在且字段完整则复用，否则进向导收集 + 写 .env。返回 creds dict。

    local 模式与 docker 模式共用此函数，统一凭据来源为 .env。
    .env 存在且四项必填凭据完整时，默认复用（避免重复输入）；缺失/不完整/不可读才进向导。
    向导收集后写 .env（覆盖前先 backup .env.bak）。
    """
    creds = None
    if ENV_FILE.exists():
        try:
            existing = load_env_file(ENV_FILE)
            if env_credentials_complete(existing):
                summarize_config(existing, source=".env")
                if confirm_fn("复用现有 .env 配置？"):
                    creds = existing
                else:
                    print("进入凭据向导重新输入（密码输入时不回显）。")
            else:
                print(".env 字段不完整，进入凭据向导补全。")
        except OSError:
            print(".env 不可读，进入凭据向导重新配置。")
    else:
        print("未找到 .env，进入凭据向导（密码输入时不回显）。")
    if creds is None:
        creds = ask_credentials(input_fn, getpass_fn)
        backup_env(ENV_FILE)
        write_env_file(ENV_FILE, creds)
    print(f".env 已就绪：账号={creds['AMAZINGDATA_USERNAME']} 服务器={creds['AMAZINGDATA_HOST']}:{creds['AMAZINGDATA_PORT']}")
    return creds


def ensure_sdk(install_fn, confirm_fn):
    try:
        import AmazingData  # noqa: F401
        return
    except ImportError:
        pass
    if confirm_fn("SDK 未安装，是否自动安装？"):
        pkgs = pick_sdk_wheels() + ["fastapi", "uvicorn[standard]", "pandas", "numpy", "tables"]
        print("正在安装 SDK 依赖（可能 1-2 分钟，请稍候）...")
        install_fn(pkgs)
        print("安装完成。")
    else:
        print("已跳过安装。请手动执行：")
        manual = " ".join(pick_sdk_wheels() + ["fastapi", "uvicorn[standard]", "pandas", "numpy", "tables"])
        print(f"  {sys.executable} -m pip install {manual}")
        sys.exit(1)


def ensure_tables(install_fn, confirm_fn):
    """确保 pytables(tables) 可 import——SDK get_adj_factor 的 HDF5 本地缓存依赖它。

    SDK whl 未声明此依赖，新环境装了 SDK 仍可能缺 tables。无论 SDK 是否已装，
    启动前都检查一次，缺了补装（老环境兜底）。
    """
    try:
        import tables  # noqa: F401
        return
    except ImportError:
        pass
    if confirm_fn("pytables(tables) 未安装（SDK 复权因子缓存需要），是否自动安装？"):
        print("正在安装 tables...")
        install_fn(["tables"])
        print("安装完成。")
    else:
        print("已跳过。请手动执行：")
        print(f"  {sys.executable} -m pip install tables")
        sys.exit(1)


def _pip_install(pkgs):
    subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], check=False)


def container_engine_available(engine="docker"):
    return shutil.which(engine) is not None


def _rc(proc):
    return proc.returncode if hasattr(proc, "returncode") else proc


def run_local(input_fn=input, getpass_fn=getpass.getpass, confirm_fn=confirm,
              install_fn=_pip_install, runner=subprocess.run):
    """本地模式：收集凭据（.env）→ 装 SDK → probe 门禁 → 起 uvicorn。

    凭据统一写 .env（与 docker 模式共用），扩展配置（ADJ_FACTOR_*、SUBSCRIPTION_* 等）
    也在 .env 里，inject_env 一次性注入 os.environ。
    probe 失败直接退出（用户修正 .env 后重跑）。
    """
    creds = _collect_credentials(input_fn, getpass_fn, confirm_fn)
    ensure_sdk(install_fn, confirm_fn)
    # 注入完整 .env（含 AUTH_TOKEN、ADJ_FACTOR_IS_LOCAL 等扩展配置），
    # 而非只注入 creds（向导路径下 creds 只有 4 项凭据 + 2 项 HTTP 配置）。
    inject_env(load_env_file(ENV_FILE))
    print("正在进行 probe 门禁验证（约数秒，请稍候）...")
    rc = run_probe(str(PROBE_REPORT), runner=runner)
    if rc != 0:
        print("probe 门禁失败，请检查凭据/网络后重跑。")
        sys.exit(1)

    ensure_tables(install_fn, confirm_fn)
    host = creds.get("HTTP_HOST", DEFAULT_HTTP_HOST)
    port = int(creds.get("HTTP_PORT", DEFAULT_HTTP_PORT))
    print(f"正在启动 uvicorn（监听 {host}:{port}，启动时会登录 SDK，约数秒）...")
    import uvicorn

    class _CleanExitServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # 触发优雅关停但不记录信号，避免 capture_signals 退出时重抛信号
            # 导致的 KeyboardInterrupt / CancelledError 异常链（Windows Ctrl+C 噪声）
            if self.should_exit and sig == signal.SIGINT:
                self.force_exit = True
            else:
                self.should_exit = True

    _CleanExitServer(uvicorn.Config("app.http_app:app", host=host, port=port)).run()


def run_container(engine="docker", input_fn=input, getpass_fn=getpass.getpass, confirm_fn=confirm,
                  runner=subprocess.run):
    """通用容器引擎启动流程（Docker 模式 2 / Podman 模式 3）。

    流程：收集凭据（.env，复用 _collect_credentials）→ build 镜像 → 容器内 probe 门禁 → compose up。
    凭据收集与 local 模式统一，避免两套配置来源。
    """
    if not container_engine_available(engine):
        engine_labels = {"docker": "Docker Desktop", "podman": "podman"}
        print(f"{engine} 未安装或未运行，请先安装 {engine_labels.get(engine, engine)}。")
        sys.exit(1)
    creds = _collect_credentials(input_fn, getpass_fn, confirm_fn)

    if confirm_fn(f"执行 {engine} build？"):
        print("正在构建镜像（首次较慢，可能数分钟）...")
        if _rc(runner(build_container_build_cmd(engine), cwd=str(PROJECT_ROOT))) != 0:
            print(f"{engine} build 失败")
            sys.exit(1)
        print("正在容器内运行 probe 门禁（约数秒）...")
        if _rc(runner(build_container_probe_cmd(PROJECT_ROOT / "docs", engine), cwd=str(PROJECT_ROOT))) != 0:
            print("probe 门禁失败：凭据可能有误，.env 已更新，建议修正后重跑")
    else:
        print("跳过 build，手动执行：")
        print(" ".join(build_container_build_cmd(engine)))

    if confirm_fn(f"执行 {engine} compose up -d？"):
        print("正在启动容器...")
        runner(build_container_compose_cmd(engine), cwd=str(PROJECT_ROOT))
    else:
        print("跳过 compose，手动执行：")
        print(" ".join(build_container_compose_cmd(engine)))

    print("手动验证：")
    print("  curl http://localhost:3021/health")
    print(f"  若返回 503：{engine} compose logs amazingdata-http 查登录错误")


def get_installed_sdk_versions():
    """返回已安装的 tgw / AmazingData 版本字典，未安装则为 None。"""
    versions = {"tgw": None, "AmazingData": None}
    for name in versions:
        try:
            import importlib.metadata as md
            versions[name] = md.version(name)
        except Exception:
            pass
    return versions


def run_install_sdk(install_fn=_pip_install, runner=subprocess.run, confirm_fn=confirm):
    """强制重装本地 wheel 到当前 Python 环境（模式 9）。"""
    try:
        wheels = pick_sdk_wheels()
    except (FileNotFoundError, ValueError) as e:
        print(f"找不到 wheel 文件：{e}")
        sys.exit(1)

    before = get_installed_sdk_versions()
    print("当前已安装版本：")
    print(f"  tgw          = {before['tgw'] or '<未安装>'}")
    print(f"  AmazingData  = {before['AmazingData'] or '<未安装>'}")
    print("将安装以下 wheel：")
    for w in wheels:
        print(f"  {Path(w).name}")

    if not confirm_fn("确认强制重装（仅这两个 wheel，依赖只补缺不重装）？"):
        print("已取消。")
        sys.exit(0)

    # --no-deps --force-reinstall：只重装这两个 wheel，不动依赖；
    # 再跑一次不带 --force-reinstall 的 install 补齐缺失依赖（已装的不重装）
    cmd1 = [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", *wheels]
    cmd2 = [sys.executable, "-m", "pip", "install", *wheels]
    print("正在重装 wheel（可能 1-2 分钟，请稍候）...")
    rc = _rc(runner(cmd1, cwd=str(PROJECT_ROOT)))
    if rc != 0:
        print("重装失败。")
        sys.exit(1)
    print("正在补齐依赖（已装的不重装）...")
    rc = _rc(runner(cmd2, cwd=str(PROJECT_ROOT)))
    if rc != 0:
        print("依赖补齐失败，SDK 已重装但依赖可能缺失。")
        sys.exit(1)

    after = get_installed_sdk_versions()
    print("安装完成，当前版本：")
    print(f"  tgw          = {after['tgw'] or '<未安装>'}")
    print(f"  AmazingData  = {after['AmazingData'] or '<未安装>'}")
    if before["tgw"] == after["tgw"] and before["AmazingData"] == after["AmazingData"]:
        print("（版本未变化，可能 wheel 与当前已装版本相同）")
    else:
        print("（版本已更新）")


def main():
    check_python_version()
    print(f"Python {sys.version.split()[0]}")
    print("选择模式：1=本地真实 SDK  2=Docker 完整链路  3=Podman 完整链路  9=更新/安装 SDK wheel")
    choice = input("模式 [1/2/3/9]: ").strip()
    if choice == "1":
        run_local()
    elif choice == "2":
        run_container("docker")
    elif choice == "3":
        run_container("podman")
    elif choice == "9":
        run_install_sdk()
    else:
        print("无效选择")
        sys.exit(1)


if __name__ == "__main__":
    main()

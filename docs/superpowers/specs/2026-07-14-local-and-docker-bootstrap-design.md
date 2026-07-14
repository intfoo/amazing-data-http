# 本地 & Docker 统一启动入口 — 设计稿

> 状态：设计已通过 brainstorming 收敛，经 subagent review 修订为最终稿。
> 日期：2026-07-14
> 范围档位：B（一键化中等改造）

## 1. 背景与问题

- **验证方式二（本地真实 SDK）**：要求用户在 PowerShell 手设 4 个环境变量，或手写 `Get-Content .env | ForEach-Object { ... }` 加载。`app/config.py` 的 `Config.from_env()` 与 `scripts/probe_sdk.py` 都只读 `os.environ`，不读 `.env` 文件。
- **验证方式三（Docker）**：要求手 `cp .env.example .env` 再编辑 4 项凭据。
- **probe 输出污染 bug**：`probe_sdk.py` 把结构化报告 `print` 到 stdout，但 SDK 的 `ad.login()` 也往 stdout 喷 TGW 噪声（`TGW Logon information...` / `logon json : {...}`）。已验证现有 `docs/probe-report.json` 前两行是噪声，**不是合法 JSON**，`json.load` 失败，无法程序化解析，README 只能让人肉眼确认。
- **`pyproject.toml` 预存矛盾**：`requires-python = ">=3.14"`，但 README 方式二以 3.13 为示例、SDK 提供 `cp313` wheel。本次顺带修正为 `>=3.13`。
- 两条验证路径各自手工、易错、无统一入口。

## 2. 目标 / 非目标

**目标**：新增统一入口 `scripts/run.py`，交互式引导用户完成本地或 Docker 两种流程，把"填凭据 + 装 SDK + probe 门禁 + 启动"压缩为一条命令；顺带修复 probe 输出污染 bug 与 `requires-python` 矛盾。

**非目标**：
- 不改业务逻辑（kline/serializer/gateway/health/errors）。
- 不改 Docker 镜像构建内容（`Dockerfile` / `docker-compose.yml` 不动）。
- 不做热重载 / 长期开发环境（C 档，本次不做）。
- 不引入 `python-dotenv`（本地用独立 JSON 文件直读注入，Docker 用原生 `env_file`）。
- 不支持 Python ≤3.12（SDK wheel 仅 `cp313` / `cp314`）。

## 3. 决策汇总（来自 brainstorming）

| 议题 | 决策 |
|---|---|
| 范围 | B 档一键化中等改造 |
| 配置 | 向导生成独立 `local.config.json`，与 `.env` 互不读取 |
| 向导验证 | 保存前 probe 登录验证，失败重输 |
| SDK 安装 | `import AmazingData` 失败时交互式询问是否自动 `pip install` |
| 统一入口 | 一个 `run.py` 覆盖本地 + Docker，用户选模式 |
| probe 频率 | 每次 `run` 都重跑 probe 门禁 |
| docker 确认 | `build` 与 `compose up` 执行前问 y/n，n 则打印命令 |
| probe 契约 | 重构 `probe_sdk.py` 输出（写文件 + 退出码 + 摘要） |

## 4. 架构

### 4.1 文件清单

**新增**
- `scripts/run.py` — 统一入口（模式选择 + 凭据向导 + 编排）
- `local.config.json` — 本地凭据文件（gitignored，向导生成）

**改动**
- `scripts/probe_sdk.py` — 输出契约重构
- `pyproject.toml` — `requires-python` 由 `">=3.14"` 改为 `">=3.13"`（修预存矛盾，使 3.13 路径下 `pip install .` 不被拦截）
- `.gitignore` — 增加 `local.config.json`
- `README.md` — 方式二 / 方式三重写为"跑 `scripts/run.py`"

**保持不变**：`app/config.py`、`app/http_app.py`、`app/gateway.py`、`app/serializer.py`、`app/kline_service.py`、`app/health.py`、`app/errors.py`、`Dockerfile`、`docker-compose.yml`、`.env.example`。

### 4.2 配置加载机制（零改动 app 代码）

**不新增** `Config.from_file`。采用**环境变量注入**：

- `run.py` 读 `local.config.json` → `os.environ[KEY] = val` 注入当前进程
- `probe_sdk.py`（subprocess 调用，继承 env）与 `uvicorn`（in-process，`Config.from_env()` 读 env）两者**零改动**即可拿到凭据
- `local.config.json` 与 `.env` 物理隔离、互不读取：本地模式只读前者，Docker 模式只写后者

> **关键约束（延迟导入）**：`uvicorn.run` 的第一个参数**必须是字符串 `"app.http_app:app"`**，不能写成 `from app.http_app import app; uvicorn.run(app, ...)`。因为 `http_app.py` 第 128 行 `app = create_app()` 在模块导入时即触发 `Config.from_env()`；若提前 import，env 尚未注入，凭据为空。字符串路径让 uvicorn 在 `inject_env()` 之后才执行 `import app.http_app`。

`local.config.json` 格式（键名即环境变量名，便于直接注入）：

```json
{
  "AMAZINGDATA_USERNAME": "45800038126",
  "AMAZINGDATA_PASSWORD": "***",
  "AMAZINGDATA_IP": "101.230.159.234",
  "AMAZINGDATA_PORT": "8600",
  "HTTP_HOST": "0.0.0.0",
  "HTTP_PORT": "3021"
}
```

- 向导只问 4 项 SDK 凭据（username / ip / port / password）；`HTTP_HOST` / `HTTP_PORT` 用默认值写入，用户可手改文件。
- 端口存为字符串（env 语义）。
- 含明文密码，与 `.env` 同等暴露面，靠 gitignore 保护，不额外加密（YAGNI）。

### 4.3 `probe_sdk.py` 输出契约重构

契约变更：

- 新增 `--out <path>` 参数，默认 `docs/probe-report.json`。
- **机器可读报告**：`json.dump(report, f, indent=2, default=str, ensure_ascii=False)` 写到 `--out` 指定文件。写文件失败（如目录不存在）时回退到 stdout 并 exit 1。
- **stdout**：仅一行人类摘要。成功：`probe OK: login=true query=true cols=<n|unknown>`（`cols` 取自 `report["df_columns"]` 长度，缺失则 `unknown`）；失败：`probe FAIL: <reason>`，`<reason>` 按路径区分：import 失败 / missing credentials / login_error / calendar_error / marketdata_error / query_error。
- **退出码**：`0` iff `report.get("login_ok") is True and report.get("query_ok") is True`，否则 `1`。`login_skipped`（凭据缺失）、import 失败等未设置 `login_ok` 的路径，`report.get("login_ok")` 为 `None` → 退出码 `1`。
- 现有全部探测逻辑（`login_params`、`period_values`、`calendar`、`query_kline`、`df_columns` / `dtypes` / `row0` 等）保留。登录成功后的所有退出路径均已调用 `_safe_logout`（已核对源码：calendar 错误 / marketdata 错误 / query 失败后 / 正常完成均调用），保持不变。
- 重构手法：把现有 **5 处 `print(json.dumps(report, indent=2, default=str)); return` + 1 处函数末尾的 `print(json.dumps(...))`（无 return）** 共 6 个收尾点，统一收敛为单一 `_finish(report, out_path) -> int`（写文件 + 打印摘要 + 返回退出码）。`probe()` 返回 `int`，`__main__` 调 `sys.exit(probe())`。
- SDK 的 stdout 噪声留在 stdout（被调用方丢弃），不进报告文件 → 修复 JSON 污染 bug。

### 4.4 `run.py` 结构（可测）

纯函数 + IO 注入分离：

| 函数 | 职责与契约 |
|---|---|
| `check_python_version() -> None` | 启动最早期调用：`sys.version_info[:2] not in {(3,13),(3,14)}` → 打印"需要 Python 3.13 或 3.14"并 `exit 1` |
| `load_local_config(path) -> dict` | 读 JSON。`JSONDecodeError` / `FileNotFoundError` 时向上抛，由编排层捕获并回退到向导流程 |
| `write_local_config(path, creds)` | 写 JSON（4 项 + 默认 host/port），`ensure_ascii=False`，缩进 2 |
| `write_env_file(path, creds)` | 生成 `.env`：键名、顺序、格式与 `.env.example` 一致（6 行：4 项 SDK 凭据由 `creds` 填入 + `HTTP_HOST=0.0.0.0` + `HTTP_PORT=3021`），末尾换行 |
| `inject_env(config: dict)` | `os.environ[k]=v`（覆盖式） |
| `pick_sdk_wheels() -> list[str]` | 按 `sys.version_info[:2]` 返回 `[tgw wheel, AmazingData cp313/cp314 wheel]`；版本不匹配时抛 `ValueError`（被 `check_python_version` 提前拦截，理论上不可达） |
| `build_docker_build_cmd() -> list[str]` | 返回 `["docker","build","--platform","linux/amd64","-t","amazingdata-http:probe","."]`（**必须含 `-t amazingdata-http:probe`**，否则后续 `docker run` 找不到镜像） |
| `build_docker_probe_cmd(docs_abs) -> list[str]` | 返回 `["docker","run","--rm","--env-file",".env","--platform","linux/amd64","-v",<docs_vol>,"amazingdata-http:probe","python","scripts/probe_sdk.py","--out","docs/probe-report.json"]`。`docs_vol` 由 `docs_abs` 经 `str(Path(docs_abs).resolve()).replace("\\","/")` 转为 Docker Desktop 兼容的正斜杠格式 |
| `build_docker_compose_cmd() -> list[str]` | 返回 `["docker","compose","up","-d"]` |
| `backup_env(path) -> None` | 若 `path`（`.env`）存在，复制为 `.env.bak`（覆盖旧备份） |
| `run_probe(out_path, runner=subprocess.run) -> int` | 以 **list 形式** `[sys.executable, "scripts/probe_sdk.py", "--out", out_path]` 调用（`cwd=项目根`，**禁用 `shell=True`**），返回 `returncode` |
| `ask_credentials(input_fn=input, getpass_fn=getpass.getpass) -> dict` | 逐项问 username / ip / port / password。每项 `strip()` 后非空校验（空则重问）；`port` 须可转 `int`（否则重问）。返回 4 项 dict（键名同环境变量名） |
| `confirm(prompt, input_fn=input) -> bool` | `input_fn().strip().lower() in ("y","yes")` 为 True，其余（含空输入/回车）为 False（默认 no） |
| `ensure_sdk(install_fn, confirm_fn) -> None` | `try: import AmazingData` 失败 → `confirm("SDK 未安装，是否自动安装？")`；y 则 `install_fn(pick_sdk_wheels() + ["fastapi","uvicorn[standard]","pandas","numpy"])`；n 则打印安装命令并 `exit 1`。注意用 `uvicorn[standard]` 而非裸 `uvicorn`，与 `pyproject.toml` 一致 |
| `run_local(...)` / `run_docker(...)` | 编排，subprocess 经 `runner` 注入便于测试；交互经 `input_fn`/`confirm_fn`/`getpass_fn` 注入 |

## 5. 流程

### 5.1 本地模式

```
python scripts/run.py
  check_python_version()                  # 非 3.13/3.14 直接 exit 1
  print(sys.version)                       # 告知当前解释器
  mode = ask("模式: 1=本地  2=Docker")

  本地分支:
    config_ok = False
    if exists(local.config.json):
        try:
            creds = load_local_config(local.config.json)
            config_ok = True
        except (JSONDecodeError, OSError):
            print "local.config.json 损坏或不可读, 进入向导重新配置"
    if not config_ok:
        for attempt in 1..3:
            creds = ask_credentials()
            ensure_sdk(...)
            inject_env(creds)
            rc = run_probe(out=docs/probe-report.json)   # Q3=A: 保存前验证
            if rc == 0: break
            print 摘要; "凭据或网络有问题, 请重试"
        else:
            exit 1
        write_local_config(local.config.json, creds)
        # 首次向导的 probe 即本次启动门禁, 不重复跑
    else:
        ensure_sdk(...)
        inject_env(creds)
        rc = run_probe(out=docs/probe-report.json)   # Q5=A: 每次 run 都重跑
        if rc != 0:
            print 摘要; exit 1

    import uvicorn
    uvicorn.run("app.http_app:app",                # 必须字符串路径, 见 §4.2
                host=creds.get("HTTP_HOST","0.0.0.0"),
                port=int(creds.get("HTTP_PORT","3021")))
```

### 5.2 Docker 模式

```
  Docker 分支:
    if not docker_available():
        print "docker 未安装或未运行" + 安装指引; exit 1
    creds = ask_credentials()               # 同一向导
    if exists(.env):
        if confirm(".env 已存在, 覆盖? (会备份为 .env.bak)"):
            backup_env(.env)
            write_env_file(.env, creds)
        else:
            skip write
    else:
        write_env_file(.env, creds)

    if confirm("执行 docker build? (y/n)"):
        rc = runner(build_docker_build_cmd())     # 失败(rc!=0)则 exit 1
        rc = runner(build_docker_probe_cmd(docs_abs))   # 自动门禁, 不询问
        if rc != 0:
            print 摘要 + "凭据可能有误, .env 已更新, 建议修正后重跑" (仍继续问 compose)
    else:
        print build_docker_build_cmd()       # 手动执行

    if confirm("执行 docker compose up -d? (y/n)"):
        runner(build_docker_compose_cmd())
    else:
        print build_docker_compose_cmd()

    print 手动验证指引:
      curl http://localhost:3021/health   # 期望 {"status":"ok"}
      curl -X POST http://localhost:3021/daily ...
      若 /health 返回 503: docker compose logs amazingdata-http 查登录错误
```

- probe 不询问（安全门禁，自动跑）；`build` / `compose up` 询问，n 则打印命令。

### 5.3 Docker probe 命令

```
docker run --rm --env-file .env --platform linux/amd64 \
  -v <abs docs, 正斜杠>:/app/docs amazingdata-http:probe \
  python scripts/probe_sdk.py --out docs/probe-report.json
```

- 显式传 `--out docs/probe-report.json`，避免依赖默认值的隐式耦合。
- 容器 `WORKDIR=/app`，`--out` 解析为 `/app/docs/probe-report.json`，挂载到宿主 `docs/`。
- `<abs docs>` 须转为正斜杠格式（`str(Path(...).resolve()).replace("\\","/")`），Docker Desktop for Windows 兼容。
- 退出码由 `docker run` 透传。
- `docs/` 不在镜像内（Dockerfile 未 COPY docs，`.dockerignore` 也排除 docs），靠 `-v` 挂载注入。

## 6. 测试

**新增 `tests/test_probe_sdk.py`**
- mock `AmazingData` 模块（`sys.modules` 注入 fake `ad`：`login` / `BaseData` / `MarketData` / `constant.Period`），调 `probe(out=path)`，断言：
  - 写出的文件是合法 JSON，内容含 `login_ok` / `query_ok`
  - 退出码：成功 → 0；login 失败 → 1；query 失败 → 1；凭据缺失（`login_skipped`）→ 1；import 失败 → 1
  - `--out` 覆盖路径生效
  - stdout 摘要格式（`probe OK: ...` / `probe FAIL: ...`）

**新增 `tests/test_run.py`**
- `write_local_config` / `load_local_config` 往返一致
- `load_local_config` 对损坏 JSON 抛 `JSONDecodeError`
- `write_env_file` 产出与 `.env.example` 键名/顺序/格式一致（逐行比较，4 项填入 + 默认 host/port）
- `build_docker_build_cmd` 含 `-t amazingdata-http:probe`
- `build_docker_probe_cmd` 含 `--out docs/probe-report.json` 且 `docs_abs` 已转正斜杠
- `build_docker_compose_cmd` 拼接正确
- `pick_sdk_wheels` 在 3.13 / 3.14 下选对 wheel，其他版本抛 `ValueError`
- `check_python_version` 在 3.12 / 3.15 下 `SystemExit`
- `inject_env` 设置 `os.environ`（测试后清理）
- `run_probe` 用 fake runner 断言 list 形式调用参数与返回码透传
- `ask_credentials` / `confirm` 用注入的 input / getpass 断言交互（含空输入重问、port 非数字重问、confirm 大小写与默认 no）
- `backup_env` 复制为 `.env.bak`

**不新增**真实 SDK / 真实 docker 集成测试，保持现有 45 用例 FakeGateway 边界。

## 7. README 改动

- **方式二**：把"6 步手工"替换为"填凭据 → `python scripts/run.py` → 选本地模式"。保留 curl 验证段作手动参考。
- **方式三**：替换为"`python scripts/run.py` → 选 Docker 模式"，说明 build/compose 会询问、`.env` 自动生成（覆盖前备份 `.env.bak`）。
- **环境变量章节**补一句：本地模式用 `local.config.json`，Docker 模式用 `.env`，二者互不读取。
- **前置条件**：Python 版本要求由"3.13 或 3.14"明确（与 `requires-python` 修正后一致）。
- **方式一**（单元测试）不动。

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 双登录（probe + uvicorn startup） | probe 走独立 subprocess，登录成功后所有退出路径均调用 `_safe_logout`（已核对源码），无状态残留；接受 |
| Windows docker 路径挂载 | `build_docker_probe_cmd` 内把 `docs_abs` 反斜杠转正斜杠，Docker Desktop 兼容 |
| probe 每次重跑开销 | 用户选定 Q5=A，每次启动多一次 SDK 登录（数秒）；接受 |
| `local.config.json` 明文密码 | 与 `.env` 同等暴露面，gitignore 保护；不加密 |
| SDK 缺失时向导无法 probe | 流程先 `ensure_sdk()` 再 probe，顺序保证 |
| `.env` 被覆写丢失旧凭据 | 覆写前 `backup_env` 备份为 `.env.bak`；probe 失败时提示用户 |
| 提前 import `http_app` 导致凭据为空 | §4.2 明确 `uvicorn.run` 必须用字符串路径；代码注释强调 |

## 9. 明确不在范围

- C 档热重载 / 日志落盘 / 三数据集（daily/adj_factor/realtime）联调切换。
- 凭据加密存储。
- Python ≤3.12 支持。
- 改 `app/*` 业务代码或 Docker 构建内容。

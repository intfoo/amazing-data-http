# tgw 登录韧性修复设计（SystemExit 兜底 / 重连退避 / stale 降级 / 失败诊断）

日期：2026-08-12
状态：已评审（用户批准方案一）
关联事故：2026-08-12 14:21 起 tgw 心跳超时 → 重连失败 → SystemExit 穿透 → 容器启动死循环

## 1. 背景与根因

### 1.1 事故时间线（日志还原）

1. 14:21:39 tgw 心跳超时断线 → `_schedule_reconnect` 触发主动重连。
2. 重连线程 `_do_login()`：`_ready=True` → 先 `_safe_logout()`（`_calendar=None`、`_ready=False` 被清空）→ `ad.login()` 阻塞 ~36s 后失败。
3. SDK `tgw_login.py:97` 登录失败路径是 `print('login fail'); exit(0)` → 抛 `SystemExit: 0`（`BaseException` 子类），`_do_login` 的 `except Exception` 捕获不到：
   - 重连线程静默死亡（日志从未出现 "tgw 主动重连失败"），状态停留在已清空；
   - 容器重启后 lifespan 的 `gateway.login()` 同样被穿透 → uvicorn "Application startup failed. Exiting"（退出码 0）→ `restart: unless-stopped` 拉起 → 启动-退出死循环。"登录失败进程仍可启动、/health 503" 的设计兜底被完全绕过。
4. 14:21:50 调度器 tick：`calendar=None` → `is_subscription_window` 直接 False → 盘中 14:21 误判"不在订阅窗口" → 停订阅、清缓存 → `/realtime` 全部 503。故障级联放大。
5. 14:21:40 起 `[kError] HandleFile | Now use ip <...> mdga.json` 每 4~5s 一条 ERROR：tgw native 层逐个试 IP 重连的信息性消息被 SDK 错标 kError，刷屏噪音。
6. 14:25:42 "Killed"：疑似 OOM（compose memory limit 2g；反复失败的 `ad.login` 在 native 层疑似泄漏连接/线程）。compose healthcheck 只标 unhealthy 不杀容器。
7. 外部根因：服务端持续 login fail。SDK 反汇编（`tgw_login.pyc`）确认失败分两类：`log_spi.max_limitation=True`（在线数超限/被踢）时 force_logout 强踢重试 5 次（间隔 2s）；否则直接失败退出。事故中每次 login 阻塞 36~50s，符合 5 次强踢重试特征，高度疑似账号在线数超限。但真实失败原因走 tgw OnLog/OnLogon 回调，而事件钩子此前在登录成功后才安装 → 启动失败时完全看不到服务端错误详情。

### 1.2 SDK 关键事实（反汇编确认）

- `tgw_login.login()` 中 `exit` 经 `LOAD_GLOBAL` 运行时解析（模块 globals 未定义 → 查 builtins）→ patch `builtins.exit` 可将"杀进程"变为抛异常，无需改 SDK。
- `set_cfg` 同为模块级函数、`LOAD_GLOBAL` 运行时解析 → 可 wrap 捕获其返回的 `log_spi`，login 失败后读 `max_limitation` 等属性分类失败原因。
- `import tgw` 不依赖登录态，事件钩子可在 login 前安装。

## 2. 目标与非目标

### 目标（P0/P1/P2 全覆盖）

1. 根除 `SystemExit` 穿透：任何路径下 SDK 登录失败 → `GatewayNotReadyError`，进程存活、/health 503、重连线程有失败日志。
2. 重连失败不清空 calendar，消除"误判不在窗口→杀订阅清缓存"的级联。
3. 重连改指数退避（降低 native 泄漏速度，OOM 防护）。
4. `/realtime` 盘中断线降级返回 stale 缓存（stale 标记），真出窗口才清缓存。
5. 抠出登录失败真实原因（max_limitation / auth / network 分类 + OnLog/OnLogon 事件），暴露到日志和 /health。
6. mdga.json 等已知噪音降级 dedup。

### 非目标（YAGNI）

- 不 vendor SDK 登录逻辑（备选方案二），不绕开 `ad.login`。
- 不改 docker 编排 / 内存限额 / healthcheck。
- 不处理 `FUND_LOCAL_PATH` 未配置警告（与本故障无关）。
- 不改 watchdog 失活重订阅的既有策略。

## 3. 详细设计

### 3.1 SystemExit 兜底（`session.py`）

评审结论：SDK 的 `exit(0)` 在调用线程同步抛 `SystemExit`，**显式 `except SystemExit` 即可完整拦截**，无需 patch `builtins.exit`（全局副作用、线程安全风险，放弃 `login_guard.py` 方案）。

`session.py::_do_login` 异常链改造：

- `except SystemExit as e`（置于 `except Exception` **之前**）：SDK login 内部 exit → 构建 `last_login_error`（category 标注 `sdk_exit`，code=e.code）→ 走统一失败处理。
- `except Exception`（现有逻辑不变）。
- 统一失败处理：`_ready=False`、已登录则 `_safe_logout()` 回滚、抛 `GatewayNotReadyError`。

效果：lifespan 的 `except Exception` 重新生效 → 启动失败进程存活；重连线程 `_do()` 的 `except Exception` 生效（SystemExit 已在 `_do_login` 内转换）→ 失败有日志。

### 3.2 失败原因提取（`tgw_events.py` + `session.py`）

- `_install_tgw_event_logger()` 调用点从 `_do_login` 末尾挪到 `ad.login` 之前（幂等标记 `_event_logger_installed` 已有，重复调用安全）。
  - **已验证可行**：`tgw/__init__.py` 末尾 `from .interface import *`，`interface.py:5` 模块级 `g_spi = TmpPushSpi()` → `import tgw` 后 `g_spi` 即存在，不依赖登录态；`tgw.Login` 内部 `IGMDApi_Init(g_spi, ...)` 后 native 日志经 `g_spi.OnLog` 分发 → login 前装钩子可捕获失败全程事件。
- 新增 `_install_login_spi_probe()`（同在 login 前安装，幂等）：
  - `from AmazingData.login import tgw_login`，wrap `tgw_login.set_cfg`：调原函数，把返回三元组中的 `log_spi` 存 `self._last_login_spi` 后原样返回。
  - import/patch 失败仅 warning，不影响主流程。
- login 失败后（`SdkLoginExitError` / `Exception` 路径）构建 `self._last_login_error`：
  - `category`：`log_spi.max_limitation` 为真 → `max_limitation`；否则按 login 窗口内捕获的 OnLog/OnLogon 事件关键词粗分 `auth`（password/credential/auth 类词）/ `network`（connect/timeout/unreachable 类词）/ `unknown`。
  - `detail`：登录窗口内的 OnLog kError 条目 + OnLogon `logon_json` 截断（500 字符），环形缓冲 20 条，存 `self._last_login_events`。
  - `ts`：失败时间戳。
- 事件缓冲由 `logged_on_log` / `logged_on_logon` 在 login 进行期间写入（`_login_in_progress` 标志由 `_do_login` 设置/清除）。
- 失败日志格式：`SDK 登录失败: category=max_limitation（在线数超限/被踢）detail=...`。
- 成功登录后清空 `_last_login_error`（保留事件缓冲供事后排查，下一次 login 开始时清空）。

### 3.3 重连指数退避（`tgw_events.py` + `base.py` + `config.py`）

- `_RECONNECT_COOLDOWN_SEC = 60` 保留为首次间隔；新增 `_RECONNECT_MAX_INTERVAL_SEC` 默认 300（env `RECONNECT_MAX_INTERVAL_SEC` 可配，写入 `Config`）。
- `_schedule_reconnect` 退避序列：60 → 120 → 240 → 300（封顶）。`_do_login` 成功时复位为 60 并重置连续失败计数。
- 失败/成功日志带 `attempt=N`、`next_retry=Ns`、`category=...`。
- 防重入逻辑（`_reconnect_in_progress`）不变。

### 3.4 calendar 保留（`session.py`）

- `_safe_logout(clear_calendar: bool = False)`：默认保留 `_calendar`（纯日期数据，当天内有效）；显式 `logout()`（shutdown 路径）传 `True`。
- `_do_login` 失败回滚调用 `_safe_logout()` 同样保留 calendar。
- 效果：重连失败期间调度器用真实日历判定窗口，不再误判。

### 3.5 /realtime stale 降级（`subscription_schedule.py` + `subscription_scheduler.py` + `realtime_service.py` + `http_app.py`）

- `is_subscription_window`：calendar 为 None/空时从"直接 False"改为走 weekday 兜底，**且尊重 `calendar_fallback_weekday` 参数**（False → 仍返回 False 严格模式），docstring 同步更新。calendar 非空但不含今天的既有兜底逻辑不变。
  - 已知行为变化（接受）：gateway 未登录时 HealthService 的 `deactivation_reason` 可能从 `inactive_offhours` 变为 `inactive_not_started`/`inactive_stale`；不影响 200/503 判定。
- 调度器 `_tick` 重排（顺序固定）：
  1. 判定 `in_window`；
  2. `in_window and not is_active()` 且 `not is_ready()` → **自愈分支**：若 `_reconnect_in_progress`（getattr 防御）为真则跳过（避免与 tgw 重连线程在 `_sdk_lock` 上 30s 竞争产生误导日志）；否则 `try: gateway.login() except Exception: debug 日志`。tick 间隔 60s 天然限频。覆盖"启动时厂商故障、事后恢复"场景——此前只能重启进程恢复；
  3. `in_window and not is_active() and is_ready()` → 启动订阅（现状）；
  4. `not in_window and is_active()` → 停订阅清缓存（现状）。
- `RealtimeService`（`_last_snapshot_ts` 已存在于 `on_snapshot` 写入，无需新增）：
  - `clear_cache` 增加重置 `_last_snapshot_ts = 0.0`。
  - 新增只读 `cache_age_sec` 属性：`_last_snapshot_ts == 0` 时返回 **None**（而非 epoch 巨值），否则 `now - _last_snapshot_ts`。
- 缓存清理策略不变：仅"真出窗口"时 `clear_cache`。断线期间窗口判定正常（3.4 保证），调度器不动订阅，缓存自然留存并随时间变 stale。
- `/realtime` 路由：
  - 缓存命中且 `cache_age_sec <= stale_threshold_sec`（90s）：原样返回 `{"data": [...]}`。
  - 缓存命中且 `stale_threshold_sec < cache_age_sec <= stale_max_age_sec`：附加 `"stale": true, "cache_age_sec": N`（纯增量，向后兼容）。
  - `cache_age_sec > stale_max_age_sec`（**stale 上限**，新增 config `STALE_MAX_AGE_SEC` 默认 300s）：视为无缓存，走 fallback；fallback 失败 → 503。防止无限期返回陈旧数据。
  - 缓存空：走既有 fallback 逻辑不变。

### 3.6 日志降噪（`tgw_events.py`）

- `logged_on_log` 的 `level == 3`（kError）分支前置噪音模式表：`"HandleFile"`、`"Now use ip"`、`mdga.json` 命中 → INFO 级 + 60s dedup。**使用独立 dedup 槽位**（新 `_last_noise_log` dict），不与 `_last_disconnect_log` 共用，避免噪音压制真实断线 WARNING。
- 模式表为模块级常量 `_TGW_NOISE_PATTERNS`，便于后续补充。

### 3.7 /health 诊断（`health.py` + `session.py`/`tgw_events.py` 暴露口）

- Gateway 新增只读属性：`last_login_error: dict | None`、`reconnect_attempts: int`，**同步声明进 `base.py` 的 `Gateway` Protocol**（否则 HealthService 经 Protocol 访问时类型检查报错）；`tests/conftest.py` 的 FakeGateway 与 `test_scheduler_calendar_refresh.py` 的独立 FakeGW 同步补齐（含 `is_ready()`）。
- （realtime 侧）`stale` 状态由 HealthService 经 RealtimeService 的 `cache_age_sec` 读取。
- `HealthService.status()` 的 sdk 段增加：`last_login_error`（category/ts/detail 截断）、`reconnect_attempts`；realtime 段增加 `stale_since`/`cache_age_sec`。
- 200/503 判定逻辑不变。

## 4. 错误处理矩阵

| 场景 | 行为 |
|------|------|
| 启动 login 失败（含 exit(0)） | 进程存活，/health 503 + last_login_error，调度器每 tick（60s）重试 login 自愈 |
| 盘中断线重连失败 | 进程存活，退避重试，/realtime 返回 stale 缓存，/daily 等 503 |
| 重连成功 | 退避复位，订阅由调度器下个 tick 自动恢复（is_active False → 启动） |
| stale 缓存超过上限（默认 300s） | /realtime 放弃缓存走 fallback，SDK 仍挂 → 503（不无限期返回陈旧数据） |
| 真出交易窗口 | 停订阅 + 清缓存（现状不变），/realtime fallback 历史快照 |
| SDK 登录成功但 BaseData 等后续失败 | `_safe_logout()` 回滚（保留 calendar）+ GatewayNotReadyError（现状保留） |
| patch set_cfg/builtins 失败 | warning 降级，核心兜底（except SystemExit）仍生效 |

## 5. 测试设计（tests/，沿用 FakeGateway 模式）

1. SystemExit 兜底：fake `ad.login` 调 `exit(0)` → 断言抛 `GatewayNotReadyError`、进程存活、`_ready=False`、`_calendar` 保留。
2. `_do_login` 各失败路径（SystemExit / Exception）统一映射 GatewayNotReadyError；失败时 `last_login_error` 已构建。
3. 退避序列：连续失败 60→120→240→300→300；成功后复位。
4. `is_subscription_window`：calendar=None + 周三 14:00 → True；calendar=None + 周六 → False；calendar=None + 工作日 16:00（出窗）→ False；calendar=None + `calendar_fallback_weekday=False` → False。
5. 调度器 not-ready 守卫：gateway 未 ready 且 `_reconnect_in_progress=False` → 尝试 `gateway.login()`（异常被吞）；`_reconnect_in_progress=True` → 跳过 login；不调 `start_subscription`。
6. stale 降级：age ≤ 90s → 无 stale 字段；90s < age ≤ 300s → `stale: true` + `cache_age_sec`；age > 300s → 走 fallback；缓存空 → fallback 行为不变。
7. 噪音过滤：构造 kError "HandleFile | Now use ip ..." → 断言 INFO 且 60s 内 dedup；噪音 dedup 不压制断线 WARNING（独立槽位）。
8. /health 新字段存在性与取值。
9. calendar 跨日残留：旧 calendar 保留 → 次日判定走 weekday 兜底正确返回（工作日 True）。
10. FakeGateway/FakeGW 同步：Protocol 新属性 + `is_ready()` 补齐后既有测试套件全绿。

## 6. 涉及文件

| 文件 | 改动 |
|------|------|
| `app/gateway/session.py` | `except SystemExit` 兜底、calendar 保留（`_safe_logout(clear_calendar)`）、login 前装钩子/probe、last_login_error 构建 |
| `app/gateway/tgw_events.py` | 退避、噪音过滤（独立 dedup 槽位）、事件缓冲、spi probe、钩子前置 |
| `app/gateway/base.py` | 退避常量、噪音模式表、Protocol 新增 `last_login_error`/`reconnect_attempts` 属性声明 |
| `app/gateway/__init__.py` | `_last_noise_log` / `_last_login_events` / `_last_login_spi` / 连续失败计数器等实例字段 |
| `app/config.py` | `RECONNECT_MAX_INTERVAL_SEC`（默认 300）、`STALE_MAX_AGE_SEC`（默认 300） |
| `app/subscription_schedule.py` | calendar None → weekday 兜底（尊重 fallback 开关） |
| `app/subscription_scheduler.py` | not-ready 自愈 login（含 `_reconnect_in_progress` 守卫）+ 启动订阅 ready 守卫 |
| `app/realtime_service.py` | `clear_cache` 重置 `_last_snapshot_ts`、新增 `cache_age_sec` |
| `app/health.py` | 诊断字段 |
| `app/http_app.py` | /realtime stale 响应字段 + stale 上限走 fallback |
| `docs/API.md` | /realtime stale/cache_age_sec 字段说明 |
| `tests/`（含 conftest FakeGateway、test_scheduler_calendar_refresh FakeGW 同步） | 上述 10 组测试 |

## 7. 风险与缓解

- **SDK 升级改 login 内部结构**：`except SystemExit` 兜底与 patch 无关，始终生效；`set_cfg` probe 失败仅 warning 降级。
- **tgw.g_spi 生命周期**：已验证 `import tgw` 即存在（`interface.py` 模块级创建，`__init__` re-export），钩子前置安全。
- **stale 缓存误导下游**：stale 标记 + cache_age_sec 显式暴露，超 300s 上限转 503；由调用方决策。
- **calendar 跨日残留**：重连成功路径 `_do_login` 会刷新 calendar；跨日后旧日历不含今天 → weekday 兜底仍正确（测试 9 覆盖）。
- **调度器自愈 login 与 tgw 重连竞争**：`_reconnect_in_progress` 守卫 + `_sdk_lock` 串行；锁竞争超时异常被吞仅 debug。

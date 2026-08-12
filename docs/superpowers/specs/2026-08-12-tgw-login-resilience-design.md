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

### 3.1 SystemExit 兜底（新 `app/gateway/login_guard.py` + `session.py`）

新增 `app/gateway/login_guard.py`：

```python
class SdkLoginExitError(Exception):
    """SDK login 内部调用 exit()/quit() 被拦截。携带 exit code。"""
    def __init__(self, code: int | None): ...

@contextmanager
def guard_sdk_exit():
    """临时替换 builtins.exit / builtins.quit 为抛 SdkLoginExitError，退出时还原。
    仅包住 ad.login() 调用窗口。线程安全性：login 全程持 _sdk_lock 串行，
    且 SDK exit 在调用线程同步抛出，patch 窗口与 login 调用同线程。"""
```

`session.py::_do_login` 改造：

- `ad.login(...)` 调用包在 `with guard_sdk_exit():` 内。
- 异常链：`except SdkLoginExitError`（拼上 `last_login_error` 诊断信息）→ `except Exception`（现有）→ 显式 `except SystemExit` 兜底转 `GatewayNotReadyError`（防其他退出路径）。
- 三种失败路径统一：`_ready=False`、已登录则 `_safe_logout()` 回滚、抛 `GatewayNotReadyError`。

效果：lifespan 的 `except Exception` 重新生效 → 启动失败进程存活；重连线程 `_do()` 的 `except Exception` 生效 → 失败有日志。

### 3.2 失败原因提取（`tgw_events.py` + `session.py`）

- `_install_tgw_event_logger()` 调用点从 `_do_login` 末尾挪到 `ad.login` 之前（幂等标记 `_event_logger_installed` 已有，重复调用安全）。
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

- `is_subscription_window`：calendar 为 None/空时从"直接 False"改为 weekday 兜底（周一~周五 + 时间窗），docstring 同步更新。calendar 非空但不含今天的既有 weekday 兜底逻辑不变。
- 调度器 `_tick` 加守卫：`not self._gw.is_ready()` 时跳过启动订阅分支（避免未登录期每 60s 刷"启动订阅失败"日志）；出窗口停订阅分支不受影响。
- **启动失败自愈**：`_tick` 在 `not is_ready()` 且 `config.is_configured()` 时尝试 `gateway.login()`（try/except 吞异常，失败只 debug 日志）。tick 间隔 60s 天然限频；与 tgw 触发的重连在 `_sdk_lock` 下串行，互不冲突。覆盖"启动时厂商故障、事后恢复"场景——此前该场景只能重启进程才能恢复。
- `RealtimeService`：
  - 新增 `_last_snapshot_ts: float | None`，`on_snapshot` 写入；`clear_cache` 重置。
  - 新增只读属性/方法暴露 `cache_age_sec`（`now - _last_snapshot_ts`，无数据为 None）。
- 缓存清理策略不变：仅"真出窗口"时 `clear_cache`。断线期间窗口判定正常（3.4 保证），调度器不动订阅，缓存自然留存并随时间变 stale。
- `/realtime` 路由：
  - 缓存命中（`data` 非空）：若 `cache_age_sec > stale_threshold_sec`（config 既有，默认 90s），响应附加 `"stale": true, "cache_age_sec": N`；新鲜时字段省略（向后兼容，响应仍为 `{"data": [...]}`）。
  - 缓存空：走既有 fallback 逻辑不变（SDK 挂时 `GatewayNotReadyError` → 503）。

### 3.6 日志降噪（`tgw_events.py`）

- `logged_on_log` 的 `level == 3`（kError）分支前置噪音模式表：`"HandleFile"`、`"Now use ip"`、`mdga.json` 命中 → INFO 级 + 60s dedup（复用 `_should_log_disconnect` 同款机制或独立 dedup 槽位）。
- 模式表为模块级常量 `_TGW_NOISE_PATTERNS`，便于后续补充。

### 3.7 /health 诊断（`health.py` + `session.py`/`tgw_events.py` 暴露口）

- Gateway 新增只读属性：`last_login_error: dict | None`、`reconnect_attempts: int`、（realtime 侧）`stale` 状态由 HealthService 经 RealtimeService 读取。
- `HealthService.status()` 的 sdk 段增加：`last_login_error`（category/ts/detail 截断）、`reconnect_attempts`；realtime 段增加 `stale_since`/`cache_age_sec`。
- 200/503 判定逻辑不变。

## 4. 错误处理矩阵

| 场景 | 行为 |
|------|------|
| 启动 login 失败（含 exit(0)） | 进程存活，/health 503 + last_login_error，调度器每 tick（60s）重试 login 自愈 |
| 盘中断线重连失败 | 进程存活，退避重试，/realtime 返回 stale 缓存，/daily 等 503 |
| 重连成功 | 退避复位，订阅由调度器下个 tick 自动恢复（is_active False → 启动） |
| 真出交易窗口 | 停订阅 + 清缓存（现状不变），/realtime fallback 历史快照 |
| SDK 登录成功但 BaseData 等后续失败 | `_safe_logout()` 回滚（保留 calendar）+ GatewayNotReadyError（现状保留） |
| patch set_cfg/builtins 失败 | warning 降级，核心兜底（except SystemExit）仍生效 |

## 5. 测试设计（tests/，沿用 FakeGateway 模式）

1. `guard_sdk_exit`：fake `ad.login` 调 `exit(0)` → 断言抛 `GatewayNotReadyError`、进程存活、`_ready=False`、`_calendar` 保留；`exit` 在 with 外还原。
2. `_do_login` 各失败路径（SdkLoginExitError / Exception / SystemExit）统一映射 GatewayNotReadyError。
3. 退避序列：连续失败 60→120→240→300→300；成功后复位。
4. `is_subscription_window`：calendar=None + 周三 14:00 → True；calendar=None + 周六 → False；calendar=None + 工作日 16:00（出窗）→ False。
5. 调度器 not-ready 守卫：gateway 未 ready 时不调 `start_subscription`，改为尝试 `gateway.login()`（异常被吞，进程不退出）。
6. stale 降级：缓存有数据且 age > threshold → /realtime 响应带 `stale: true`；新鲜 → 无该字段；缓存空 → fallback 行为不变。
7. 噪音过滤：构造 kError "HandleFile | Now use ip ..." → 断言 INFO 且 60s 内 dedup。
8. /health 新字段存在性与取值。

## 6. 涉及文件

| 文件 | 改动 |
|------|------|
| `app/gateway/login_guard.py` | 新增：SdkLoginExitError + guard_sdk_exit |
| `app/gateway/session.py` | exit guard 包裹 login、异常链、calendar 保留、login 前装钩子/probe、last_login_error 构建 |
| `app/gateway/tgw_events.py` | 退避、噪音过滤、事件缓冲、spi probe、钩子前置 |
| `app/gateway/base.py` | 退避常量、噪音模式表 |
| `app/config.py` | `RECONNECT_MAX_INTERVAL_SEC` |
| `app/subscription_schedule.py` | calendar None → weekday 兜底 |
| `app/subscription_scheduler.py` | not-ready 守卫 |
| `app/realtime_service.py` | `_last_snapshot_ts` / cache_age_sec |
| `app/health.py` | 诊断字段 |
| `app/http_app.py` | /realtime stale 响应字段 |
| `tests/` | 上述 8 组测试 |

## 7. 风险与缓解

- **builtins patch 线程安全**：patch 窗口仅 login 调用期间，login 全程持 `_sdk_lock` 串行；其他线程若在窗口内调 `exit()` 会抛 SdkLoginExitError——正常代码路径无此调用，可接受。
- **SDK 升级改 login 内部结构**：`except SystemExit` 显式兜底保证即使 patch 失效进程也不死；probe 失败仅 warning。
- **stale 缓存误导下游**：stale 标记 + cache_age_sec 显式暴露，由调用方决策；默认阈值 90s 与 watchdog 口径一致。
- **calendar 跨日残留**：重连成功路径 `_do_login` 会刷新 calendar；真出窗口清缓存逻辑不受 calendar 残留影响（日历当天有效，跨日后 weekday 兜底仍正确）。

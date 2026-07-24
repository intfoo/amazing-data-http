# 订阅生命周期管理设计

**日期**：2026-07-22
**状态**：已评审修订（v2），待实现
**背景**：生产容器 `amazing-data-http` 在无业务调用、仅健康检查的情况下，CPU 恒定 ~16%（≈1 核）、内存 1.5GB 不掉；且 SDK 会话被踢后无任何日志告警，/health 仍报 active（假活）。

**修订记录**：
- v2（2026-07-22）：根据设计评审修复 6 个 BLOCKER（calendar 类型/Protocol 缺失/线程安全/HealthService 改造缺失/deactivation reason 三态/watchdog 首次盲区）+ 6 个 MAJOR（配置化/systemd 前提/向后兼容/start_watchdog 时机/订阅恢复/shutdown 安全）。

## 1. 问题陈述

### 1.1 资源占用异常
容器全天 CPU ~16%（≈1 核）、内存 1.5GB，非交易时段也不下降。第一性原理排查（`top -H` + 本地 SDK 对照实验）结论：

- **CPU 大头在 tgw 原生线程**（接收/解码/保活），py-spy 看不见，`top -H` 显示 46 线程。
- **对照实验实锤**：500 只股票订阅，零回调纯会话维持 = 0.972 核；7935 次回调全量处理 = 0.972 核。**回调处理边际成本≈0，~1 核是订阅会话的固定维持成本**。
- **规模无关**：实验 500 只（0.97 核）vs 生产全市场 5500+ 只（0.94 核），CPU 几乎无差别。固定成本是会话级的。

### 1.2 假活问题
SDK 会话被踢时，`SubscribeData.run()` 静默不抛异常 → `RealtimeService._active` 永远 True → /health 报 `realtime: active`，但 /realtime 返回空，**零告警日志**。代码无任何 `last_snapshot_time` / watchdog / heartbeat 机制（lexis 确认）。

### 1.3 僵死问题
生产日志显示：21:20 后连 30s 一次的 healthcheck 日志都消失，但容器主进程未退出，直到次日 11:22 才终止（inspect `FinishedAt`）。`restart: unless-stopped` 对"僵死但不退出"无效。

## 2. 目标与非目标

### 目标
1. 非交易时段不持有订阅会话，CPU 降 ~60%（每日省 ~16 小时 × 1 核）。
2. 订阅失活时 60s 内产生 error 日志 + /health 反映真实状态（不再假活）。
3. 容器僵死时自动重启，最长僵死窗口 < 5 分钟。
4. 每日定时重启释放 1.5GB 内存，防缓慢性增长。

### 非目标
- 不优化 SDK 内部 / tgw 原生层（实验证伪：无可优化空间）。
- 不减少订阅范围（实验证伪：CPU 与规模无关）。
- 不改 `SetDfFormat`（实验证伪：SDK 默认已 False，强制 True 破坏回调）。
- 不解决 SDK/tgw 僵死的根因（那是独立课题，本设计只兜底）。

## 3. 设计总览

三组件，互为补充：

```
组件 A（应用代码）：分时段订阅 —— 决定"要不要开订阅"
组件 B（应用代码）：存活检测    —— 发现"订阅悄悄死了"
组件 C（部署配置）：定时重启+僵死兜底 —— 强制刷新 + 处理僵死
```

## 4. 组件 A：分时段订阅

### 4.1 Config 扩展
`app/config.py` 新增可配置字段（遵循现有 frozen dataclass + env 模式）：

```python
@dataclass(frozen=True)
class Config:
    # ... 现有字段 ...
    subscription_open: str = "09:00"        # 订阅窗口开始 HH:MM
    subscription_close: str = "15:20"       # 订阅窗口结束 HH:MM
    stale_threshold_sec: int = 90           # watchdog 失活阈值
    watchdog_interval_sec: int = 60         # watchdog 检查间隔

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            # ... 现有字段 ...
            subscription_open=os.getenv("SUBSCRIPTION_OPEN", "09:00"),
            subscription_close=os.getenv("SUBSCRIPTION_CLOSE", "15:20"),
            stale_threshold_sec=int(os.getenv("STALE_THRESHOLD_SEC", "90")),
            watchdog_interval_sec=int(os.getenv("WATCHDOG_INTERVAL_SEC", "60")),
        )
```

### 4.2 Gateway Protocol 扩展
`app/gateway.py` 的 `Gateway` Protocol 新增 `calendar` 属性，`AmazingDataGateway` 新增 property：

```python
@runtime_checkable
class Gateway(Protocol):
    # ... 现有方法 ...
    @property
    def calendar(self) -> list[int] | None: ...

class AmazingDataGateway:
    @property
    def calendar(self) -> list[int] | None:
        return self._calendar  # login 前为 None，login 后为 list[int]
```

`tests/conftest.py` 的 `FakeGateway` 同步新增：

```python
class FakeGateway:
    def __init__(self, ..., calendar: list[int] | None = None):
        self._calendar = calendar  # 默认 None；测试可注入 [20240102, ...]

    @property
    def calendar(self) -> list[int] | None:
        return self._calendar
```

### 4.3 时段判定
新增 `app/subscription_schedule.py`：

```python
import datetime

def parse_hhmm(s: str) -> datetime.time:
    """'09:00' → datetime.time(9, 0)"""
    h, m = s.split(":")
    return datetime.time(int(h), int(m))

def is_subscription_window(
    now: datetime.datetime,
    calendar: list[int] | None,
    open_time: str = "09:00",
    close_time: str = "15:20",
) -> bool:
    """是否应启动订阅：交易日 且 在 [open_time, close_time] 窗口内。

    calendar 为 None 或空时返回 False（login 前 / 无日历数据）。
    """
    if not calendar:
        return False
    today_int = int(now.strftime("%Y%m%d"))
    if today_int not in calendar:
        return False
    t = now.time()
    return parse_hhmm(open_time) <= t <= parse_hhmm(close_time)
```

注：`set(calendar)` 每次重建开销极小（~250 元素，微秒级），healthcheck 每 30s 调用一次，无需优化。

### 4.4 lifespan 集成
`app/http_app.py` 的 `lifespan` 改造：

```python
if config.is_configured():
    gateway.login()
    cal = gateway.calendar
    if cal and is_subscription_window(
        datetime.datetime.now(), cal,
        open_time=config.subscription_open,
        close_time=config.subscription_close,
    ):
        # 现有 _init_subscription 流程（内部 start_watchdog 也需要 cal）
        app.state.subscription_thread = threading.Thread(
            target=_init_subscription, args=(cal,), daemon=True, name="sub-init"
        )
        app.state.subscription_thread.start()
    else:
        logger.info("outside subscription window, skipping subscription (SDK query still available)")
```

### 4.5 非窗口期行为
- SDK 仍 login，`/daily` `/minute` `/adj_factor` 正常工作（这些是按需查询，零常驻 CPU）。
- `/realtime` 订阅缓存为空 → 走现有 fallback（显式 codes 查 query_snapshot，无 codes 返回空/旧缓存）。语义与当前非交易时段一致。

## 5. 组件 B：订阅存活检测

### 5.1 RealtimeService 改造
`app/realtime_service.py`：

```python
import datetime
import time
import threading

# 模块级默认值（实际从 config 传入，此处仅文档参考）
STALE_THRESHOLD_SEC = 90   # 盘中快照 3s/轮，90s=30 轮无数据即判失活
WATCHDOG_INTERVAL_SEC = 60

class RealtimeService:
    def __init__(self, gateway):
        # ... 现有字段 ...
        self._last_snapshot_ts: float = 0.0
        self._deactivation_reason: str | None = None  # "stale" / "error" / None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_start_ts: float = 0.0  # watchdog 启动时间，用于首次数据超时检测
        self._stop_flag = threading.Event()    # watchdog 优雅停止信号

    # ---- 线程安全说明 ----
    # _active: bool 和 _last_snapshot_ts: float 在 CPython GIL 下单个读写是原子的，
    # 不会出现部分写入。多线程并发读写的最坏情况是"读到旧值再下一次读到新值"，
    # 对本场景（watchdog 判定 + health 报告）无正确性影响（最终一致）。
    # 项目使用 Python 3.14（GIL 模式），无需额外加锁。
    # 若未来迁移到 free-threading（no-GIL），需用 threading.Lock 保护。

    def on_snapshot(self, data) -> None:
        # ... 现有逻辑 ...
        self._last_snapshot_ts = time.time()
        # 自动恢复：若订阅曾被标记 inactive 但数据又来了，说明已恢复
        if not self._active:
            self._active = True
            self._deactivation_reason = None
            logger.info("subscription recovered: data received, reactivating")

    def on_subscription_error(self, err=None) -> None:
        self._active = False
        self._deactivation_reason = "error"
        logger.error("realtime subscription deactivated due to error: %s", err)

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._deactivation_reason = None

    def deactivation_reason(self) -> str | None:
        """供 HealthService 区分 inactive_stale / inactive_not_started / inactive_error"""
        return self._deactivation_reason

    def last_snapshot_ts(self) -> float:
        return self._last_snapshot_ts

    def start_watchdog(
        self,
        calendar: list[int],
        stale_threshold_sec: int = STALE_THRESHOLD_SEC,
        watchdog_interval_sec: int = WATCHDOG_INTERVAL_SEC,
        open_time: str = "09:00",
        close_time: str = "15:20",
    ) -> None:
        """启动后台 watchdog 线程。lifespan 订阅启动后调用。"""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._stop_flag.clear()
        self._watchdog_start_ts = time.time()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            args=(calendar, stale_threshold_sec, watchdog_interval_sec, open_time, close_time),
            daemon=True, name="sub-watchdog"
        )
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        """优雅停止 watchdog。lifespan shutdown 时调用。"""
        self._stop_flag.set()

    def _watchdog_loop(
        self, calendar, stale_threshold_sec, watchdog_interval_sec, open_time, close_time
    ) -> None:
        while self._active and not self._stop_flag.is_set():
            # 用 Event.wait 替代 time.sleep，可被 stop_watchdog 立即唤醒
            if self._stop_flag.wait(timeout=watchdog_interval_sec):
                break
            if not self._active:
                break
            now = datetime.datetime.now()
            if not is_subscription_window(now, calendar, open_time, close_time):
                continue  # 非窗口期不判 stale（盘后无数据正常）
            if self._last_snapshot_ts == 0:
                # 还没收到过数据：检查是否超过启动后阈值（首次数据超时）
                elapsed_since_start = time.time() - self._watchdog_start_ts
                if elapsed_since_start > stale_threshold_sec:
                    logger.error(
                        "subscription started but no data received for %.0fs, marking inactive",
                        elapsed_since_start,
                    )
                    self._active = False
                    self._deactivation_reason = "stale"
                    break
                continue
            elapsed = time.time() - self._last_snapshot_ts
            if elapsed > stale_threshold_sec:
                logger.error(
                    "subscription stale: no data for %.0fs during trading hours, marking inactive",
                    elapsed,
                )
                self._active = False
                self._deactivation_reason = "stale"
                break
```

### 5.2 /health 语义升级
`app/health.py` 的 `HealthService` 改造：

```python
import datetime

class HealthService:
    def __init__(self, config, gateway, realtime_service=None):
        self._config = config
        self._gw = gateway
        self._realtime_svc = realtime_service

    def _realtime_detail(self) -> str:
        """计算 realtime_detail 三态/四态。"""
        rt_svc = self._realtime_svc
        if not rt_svc:
            return "unavailable"
        if rt_svc.is_active():
            return "active"
        # inactive 分三种情况
        now = datetime.datetime.now()
        cal = self._gw.calendar
        if not cal or not is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
        ):
            return "inactive_offhours"  # 非窗口期，inactive 是正常的
        # 窗口期内 inactive
        reason = rt_svc.deactivation_reason()
        if reason == "stale":
            return "inactive_stale"
        if reason == "error":
            return "inactive_error"
        if rt_svc.last_snapshot_ts() == 0:
            return "inactive_not_started"
        return "inactive_stale"  # 有过数据但无明确 reason，按 stale 处理

    def status(self) -> dict:
        ready = self._config.is_configured() and self._gw.is_ready()
        rt_detail = self._realtime_detail()
        rt = rt_detail == "active"
        if not self._config.auth_required:
            auth_state = "disabled"
        elif self._config.is_auth_valid():
            auth_state = "configured"
        else:
            auth_state = "misconfigured"
        return {
            "status": "ok" if (ready and self.is_ok()) else "degraded",
            "sdk": "ready" if self._gw.is_ready() else "not_ready",
            "config": "complete" if self._config.is_configured() else "incomplete",
            "realtime": "active" if rt else "inactive",
            "realtime_detail": rt_detail,
            "auth": auth_state,
        }

    def is_ok(self) -> bool:
        """是否健康（决定 /health 返回 200 还是 503）。"""
        if not (self._config.is_configured() and self._gw.is_ready()):
            return False
        # 窗口期内要求 realtime 活跃；非窗口期不要求
        now = datetime.datetime.now()
        cal = self._gw.calendar
        if cal and is_subscription_window(
            now, cal,
            open_time=self._config.subscription_open,
            close_time=self._config.subscription_close,
        ):
            return self._realtime_svc.is_active() if self._realtime_svc else False
        return True
```

| 状态 | realtime | realtime_detail | /health HTTP |
|---|---|---|---|
| 订阅活跃且在窗口期内有数据 | active | active | 200 |
| 非窗口期（正常不订阅） | inactive | inactive_offhours | 200 |
| 窗口期内订阅失活（stale） | inactive | inactive_stale | **503** |
| 窗口期内订阅报错（error） | inactive | inactive_error | **503** |
| 窗口期内订阅未启动 | inactive | inactive_not_started | **503** |

503 触发 healthcheck 失败 → 组件 C 的僵死兜底重启。

### 5.3 lifespan 集成
`start_watchdog` 在 `_init_subscription` 函数内、`set_active(True)` 之后调用（确保 watchdog 启动时 `_active=True`）：

```python
def _init_subscription(cal):
    code_list = gateway.get_realtime_code_list()
    gateway.start_snapshot_subscription(
        code_list,
        on_data=realtime_service.on_snapshot,
        on_error=realtime_service.on_subscription_error,
    )
    realtime_service.set_active(True)
    realtime_service.start_watchdog(
        cal,
        stale_threshold_sec=config.stale_threshold_sec,
        watchdog_interval_sec=config.watchdog_interval_sec,
        open_time=config.subscription_open,
        close_time=config.subscription_close,
    )
```

shutdown 时增加 `realtime_service.stop_watchdog()`（watchdog 是 daemon 线程，`stop_watchdog` 通过 Event 立即唤醒，无需 join）：

```python
# lifespan shutdown
realtime_service.stop_watchdog()
sub_thread = getattr(app.state, "subscription_thread", None)
if sub_thread and sub_thread.is_alive():
    sub_thread.join(timeout=10)
gateway.stop_subscription()
gateway.logout()
```

## 6. 组件 C：定时重启 + 僵死兜底

### 6.1 定时重启（systemd timer）
两个 systemd unit 文件（部署到 `/etc/systemd/system/`）：

```ini
# amazing-data-http-restart-open.service
[Unit]
Description=Restart amazing-data-http (enter subscription mode)
[Service]
Type=oneshot
ExecStart=/usr/bin/podman restart amazing-data-http
User=deployer

# amazing-data-http-restart-open.timer
[Unit]
Description=Restart amazing-data-http at market open
[Timer]
OnCalendar=Mon..Fri 08:55:00
Persistent=false
[Install]
WantedBy=timers.target
```
（`-close.timer` 同理，`OnCalendar=Mon..Fri 15:25:00`）

**部署前提**：
- 宿主机必须是物理机或 VM，运行 systemd，部署者有 `/etc/systemd/system/` 写入权限。
- `deployer` 用户需有 podman 权限（subuid/subgid 配置或 sudo）。
- `Persistent=false`（已从 `true` 改为 `false`）：避免宿主机宕机恢复后补执行非预期重启。

**时段关系**：15:20 关闭订阅窗口（`is_subscription_window` 返回 False，不订阅新数据），15:25 重启容器。5 分钟缓冲确保盘后收尾数据处理完毕，重启时 `stop_subscription()` 已清理会话，重启无副作用。08:55 重启 → 09:00 窗口开启前完成 login + 订阅初始化。

**替代方案（若宿主机无 systemd）**：容器内用 `threading.Timer` 触发 `os.kill(os.getpid(), signal.SIGTERM)`，配合 `restart: unless-stopped` 实现定时重启。优点是无宿主机依赖；缺点是应用与重启逻辑耦合、调试链长。当前设计选 systemd timer，若部署环境不支持 systemd 则降级为此方案。

### 6.2 僵死兜底
podman 4.x 的 `--health-on-failure=restart`：healthcheck 连续失败时自动重启容器。

- 若 podman ≥ 4.4：用 Quadlet 重写部署，`.container` 文件加 `HealthOnFailure=restart`。
- 若 podman < 4.4 或保持 docker-compose.yml：加外部 watchdog systemd timer（每 5 分钟检查 `podman inspect --format '{{.State.Health.Status}}' amazing-data-http`，unhealthy 则 restart）。

**前提**：healthcheck 必须能检测僵死。当前 `python -c "urllib.request.urlopen('/health')"` 在 uvicorn event loop 卡死时会超时失败 → 触发 unhealthy → 重启。组件 B 的 503 也走这条路径。

### 6.3 容器资源限额
`docker-compose.yml` 加：
```yaml
deploy:
  resources:
    limits:
      cpus: '1.5'
      memory: 2g
```
（podman-compose 兼容性需实测；不兼容则用 podman 原生 `--cpus=1.5 --memory=2g`。）保护 3.7GB 宿主机不被单容器吃光。

## 7. 错误处理与边界

| 场景 | 处理 |
|---|---|
| 启动时非交易日/非窗口期 | 跳过订阅，SDK 仍 login，查询接口正常 |
| 启动时在窗口期但 login 失败 | 现有逻辑：log error，/health 503，进程不退出 |
| 订阅初始化失败（get_realtime_code_list / start_snapshot_subscription） | 现有逻辑：log error，`_active` 保持 False，/health 503（`inactive_not_started`） |
| 盘中订阅 stale | watchdog 置 `_active=False` + `_deactivation_reason="stale"` + log error，/health 503 → 触发重启 |
| 盘中订阅 error | `on_subscription_error` 置 `_active=False` + `_deactivation_reason="error"` + log error，/health 503 |
| 盘中订阅 stale 后 SDK 自动恢复推送 | `on_snapshot` 检测 `_active=False` 时自动恢复 `_active=True` + log info，/health 回 200，避免单次抖动导致不必要重启 |
| watchdog 首次数据超时 | 订阅启动后 `stale_threshold_sec` 内未收到任何数据 → 置 inactive + reason="stale"，/health 503 |
| 盘中容器僵死 | healthcheck 超时 → unhealthy → `--health-on-failure` 重启 |
| 15:25 重启进入空闲模式 | 跳过订阅，1.5GB 内存释放，CPU 降 ~60% |
| 次日 08:55 重启 | 进入订阅模式，重新 login + 订阅 |
| watchdog 误报（盘中偶发网络抖动 90s 无数据） | 置 inactive 触发重启；若 SDK 自动恢复则 on_snapshot 恢复 _active；重启成本可接受（秒级），宁可误重启不可假活 |
| calendar 为 None（login 前 / logout 后） | `is_subscription_window` 返回 False，不启动订阅，watchdog 不判定 stale |

## 8. 测试策略

### 单元测试（新增 `tests/test_subscription_schedule.py`）
- `is_subscription_window`：交易日盘前/盘中/盘后/边界、非交易日、calendar 为 None、calendar 为空列表
- `parse_hhmm`：正常解析、边界值

### 单元测试（扩展 `tests/test_realtime_service.py`）
- watchdog stale 判定：mock `_last_snapshot_ts`，验证窗口期内超阈值置 inactive + reason="stale" + log
- watchdog 首次数据超时：`_last_snapshot_ts=0`，watchdog 启动后超过阈值 → 置 inactive
- watchdog 非窗口期不触发：非交易日/非窗口期不判 stale
- `on_snapshot` 自动恢复：`_active=False` 时收到数据 → `_active=True` + reason=None
- `deactivation_reason()` 返回值：stale / error / None 三态
- `stop_watchdog()`：Event.set 后 watchdog 线程在 `watchdog_interval_sec` 内退出

### 单元测试（新增 `tests/test_health.py`）
- `_realtime_detail()` 五态：active / inactive_offhours / inactive_stale / inactive_error / inactive_not_started
- `is_ok()` 窗口期 vs 非窗口期：窗口期内 inactive → False（503），非窗口期 inactive → True（200）
- `is_ok()` calendar=None → True（只要 config + gateway OK）

### 集成测试（扩展 `tests/test_http_app.py`）
- FakeGateway 模拟订阅启动后无回调，验证 watchdog 标记 inactive + /health 返回 503 + `realtime_detail=inactive_stale`
- 非窗口期启动：验证不调 `start_snapshot_subscription`，/health 返回 200 + `realtime_detail=inactive_offhours`
- FakeGateway 增加 `calendar` 属性（注入交易日历 + 窗口时间参数）

### 手动验证（部署后）
- 观察 08:55 / 15:25 重启日志（journalctl）
- 盘中人为停掉订阅（重连踢线）：确认 60-90s 内 log error + /health 503 + 自动重启
- 盘中短暂断线后恢复：确认 on_snapshot 自动恢复 _active + /health 回 200（不重启）
- 非交易时段 CPU 应 < 2%（仅 uvicorn + 偶发 /daily 查询）

## 9. 已否决方案（实验记录，避免重蹈）

| 方案 | 否决理由 | 实验证据 |
|---|---|---|
| `SetDfFormat(False)` 优化 | SDK `run()` 硬编码已调 `SetDfFormat(False)`，生产本就运行在 False 模式；强制 True 直接零回调 | 本地 venv 对照实验 2026-07-22，B 组强制 True → 0 回调 |
| 减少订阅范围降 CPU | CPU 与订阅规模无关，固定成本是会话级 | 实验 500 只 0.97 核 vs 生产 5500+ 只 0.94 核 |
| 应用内定时 SIGTERM 自杀 | 调试链长、与应用耦合 | — |
| 优化 `on_snapshot` 回调 | 回调处理边际成本≈0，非 CPU 大头 | 实验 A 组 7935 回调 0.97 核 ≈ B 组零回调 0.97 核 |

## 10. 已决议问题

1. **podman 版本**：实现前先 `podman --version` 确认。≥4.4 用 Quadlet（`.container` 文件 + `HealthOnFailure=restart`）；<4.4 或保持 docker-compose.yml 则加外部 watchdog systemd timer。
2. **订阅窗口边界 09:00/15:20**：09:00 开始覆盖集合竞价（09:15-09:25）。09:00-09:15 调 /realtime 拿到空缓存是预期行为（SDK 此时段无推送）。窗口时间已可配置化（env `SUBSCRIPTION_OPEN` / `SUBSCRIPTION_CLOSE`），无需提前。
3. **watchdog 阈值 90s**：已可配置化（env `STALE_THRESHOLD_SEC`）。90s=30 轮无数据，盘中快照 3s/轮。可运行后根据实际抖动频率调参。
4. **节假日处理**：systemd timer 按工作日（Mon-Fri）触发，节假日容器重启后 `is_subscription_window` 判定非交易日→空闲模式，无害但有一次无意义重启。可接受。
5. **线程安全**：CPython GIL 下 `_active`/`_last_snapshot_ts` 单值读写原子，本场景最终一致即可。已用 `threading.Event` 实现 watchdog 优雅停止。若未来迁移 free-threading 需加 Lock。
6. **订阅恢复**：`on_snapshot` 中检测 `_active=False` 自动恢复为 True，避免单次网络抖动导致不必要的容器重启。

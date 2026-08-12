# 架构审查修复设计（2026-08-12）

## 背景

对 amazingDataHttp 适配服务的全局架构审查产出 13 项发现，经逐条二次核验：12 项属实（#6/#8 降级为低优先/防御性），#11（认证无限流）判定为部署假设、明确不修。用户确认范围 A：全部 12 项属实项均修复。

## 总体约束

- 不改变任何端点的请求/响应契约（`docs/API.md` 不变，除新增 422 场景与 gzip 透明压缩）。
- 现有 107 个测试用例必须全绿；每项修复配套新增/更新测试。
- 所有改动保持 FakeGateway 可测性，不引入真实 SDK 依赖。
- 终端纪律：pytest 完整套件最多跑 2 次（全量 + 受影响文件复跑）。

## 逐项设计

### #3 fallback 缓存 codes 维度（正确性 bug，P0）

文件：`app/realtime_service.py`

现状：`fallback_snapshot` 的 TTL 缓存 `_fallback_cache` 不记录查询的 codes 集合。请求 A 查 `["X"]` 填充缓存后，TTL 内请求 `["Y"]` 在旧缓存上过滤返回 `[]`，调用方无法区分"无数据"与"缓存未覆盖"。

修复：
- 新增 `self._fallback_codes: set[str]`，与 `_fallback_cache` 同生命周期。
- TTL 命中条件改为 `set(codes) ⊆ _fallback_codes`；不满足则视为 miss，进入 singleflight 查询。
- 新查询结果按 code **合并**进缓存（内部用 dict[code, record] 去重后重建 list），`_fallback_codes |= set(codes)`——避免 A→B→A 振荡反复查 SDK。
- 无 codes 分支行为不变（不触发查询，仅过滤现有缓存）。
- 写序保证：先赋值 `_fallback_cache` 再更新 `_fallback_codes`（引用赋值原子；TTL 命中路径若读到旧 `_fallback_codes` 只会误判 miss 多查一次，无正确性问题）。`_filter_fallback` 只读 `_fallback_cache` 快照，不读 `_fallback_codes`。

### #2 超时幽灵线程（P0）

文件：`app/gateway/resilience.py`、`app/gateway/query_basedata.py`（仅 `get_adj_factor` 使用 `_call_sdk_with_timeout`）

现状：超时后 daemon 线程仍在 SDK native 层执行，但 `gateway._lock` 已释放，下一调用与幽灵线程并发进入非线程安全的 SDK。

修复：
- `_call_sdk_with_timeout` 超时路径追加两步：置 `self._ready = False`（后续查询立即 503，不再并发进 SDK）；调用既有 `self._schedule_reconnect("sdk call timeout: <label>")` 走退避重建会话（`_schedule_reconnect` 是异步启动 daemon 线程，当前持锁线程不阻塞；重连线程在 `_sdk_lock` 上最多等 30s，当前线程异常传播释放锁后即获锁重连，非死锁）。
- **超时异常消息必须避开连接关键词**：现有消息含 "timed out"，会被 `get_adj_factor` 的 `except` 经 `_is_connection_error` 匹配 "timeout" → 在 `_ready=False` 状态下触发 `_do_login()` + 重试。改为中文消息（如 `{label} 超过 {timeout}s 无响应（SDK 线程已隔离为 daemon）`），使 `_is_connection_error`/`_is_sdk_corruption` 均不匹配，异常直接映射 502，重连由 `_schedule_reconnect` 统一负责。
- 幽灵线程在旧会话对象上自然终结（与断线重连场景等价）。`_do_login` 在 `_ready=False` 下跳过 `_safe_logout` 直接重新 login，行为正确。
- `_call_sdk_with_timeout` 由 `@staticmethod` 改为实例方法（访问 `_schedule_reconnect`/`_ready`）；`query_basedata.py` 3 处调用已是 `self.` 形式，签名兼容。MRO 上 `ResilienceMixin` 与 `TgwEventMixin` 同组合于 `AmazingDataGateway`，`_reconnect_lock` 等字段在 `__init__` 已初始化，无初始化顺序问题。
- **必须同步更新现有测试** `test_amazingdata_gateway.py:233-250` 的 `test_call_sdk_with_timeout_raises_on_hang`/`test_call_sdk_with_timeout_passthrough_error`：类方式调用 `AmazingDataGateway._call_sdk_with_timeout(...)` 改为实例调用。
- 预期行为：`/adj_factor` 超时后返回 502，服务短暂 503，重连受 `_schedule_reconnect` 退避策略控制（首次冷却 60s），非立即重连。

### #4 etf_flow 缓存（P0）

文件：`app/etf_flow_service.py`、`app/config.py`

现状：每次请求 `get_code_info` + `get_fund_share` + `get_fund_nav` 三连远程拉取，串行各持一次全局锁，单次请求持锁可达数十秒。

修复：
- 宽基 ETF 清单（codes + name_map）按日缓存：key=当日日期字符串，跨日自动失效。
- 查询结果缓存：key=(start_time, end_time)，TTL 由新增配置 `ETF_FLOW_CACHE_TTL_SEC`（默认 300s；份额 T+1 更新，5 分钟无 freshness 风险）。
- 缓存读写加 `threading.Lock`（service 方法经 `asyncio.to_thread` 跑在线程池）。
- 缓存命中路径零 SDK 调用、零持锁。

### #1 SdkGate 语义（P1）

文件：`app/config.py`

- `SDK_MAX_CONCURRENT` 默认值 5→2（1 执行 + 1 吸收突发）。注释写明"SDK 调用全局串行，并发上限只决定排队深度"。
- `SDK_LOCK_TIMEOUT_SEC` 30s 不动（缩短会让排在长查询后的合法请求误死）。
- 同步更新：`test_config.py:39-42` 默认值断言 5→2；`.env.example` 注释值；`README.md` 配置表默认值列。

### #9 规模上限 + 压缩（P1）

文件：`app/http_app.py`

- `DailyRequest`/`MinuteRequest`/`AdjFactorRequest` 的 `codes` 校验追加上限：模块常量 `MAX_CODES = 500`，超限抛 ValueError（pydantic → 422），错误消息提示分批。
- 添加 `GZipMiddleware(minimum_size=1024)`（starlette 内置，`from starlette.middleware.gzip import GZipMiddleware`）。**中间件顺序**：`GZipMiddleware` 在 `AuthMiddleware`/`RequestIdMiddleware` 之后 `add_middleware`（insert(0) 语义下后 add = 最外层最先执行），压缩位于传输最外层。/realtime 全市场与大 kline 响应透明压缩。

### #5 时区显式化（P1）

文件：`app/subscription_scheduler.py`、`app/realtime_service.py`、`app/health.py`、`app/etf_flow_service.py`、`pyproject.toml`

- 所有 naive `datetime.datetime.now()` / `datetime.now()` 改为 `datetime.datetime.now(ZoneInfo("Asia/Shanghai"))`，含 `etf_flow_service.py:161` 默认区间计算（`:211` 的 `_dt.strptime` 不涉及当前时间，可不动；为一致性可一并 localize，但非必须）。
- `is_subscription_window` 内部只用 `.time()/.weekday()/.strftime()`，aware 对象零改动兼容，函数签名不变。
- `pyproject.toml` 增加 `tzdata` 依赖（Windows 本地 zoneinfo 需要；Docker 有系统 tzdata，纯保险）。

### #8 asdict 浅拷贝（P1，防御性）

文件：`app/realtime_service.py`

- `_build_extract_fn` 的 dataclass 分支由 `dataclasses.asdict`（递归深拷贝）改为
  `lambda d: {f.name: getattr(d, f.name) for f in dataclasses.fields(d)}`。
- 前提假设（注释写明）：SDK Snapshot 字段全为标量/datetime（probe 样本证实扁平结构）；逐值 `serialize_value` 兜底不变。

### #6 watchdog 代际（P2）

文件：`app/realtime_service.py`

- `stop_watchdog` 在 set flag 后 `join(timeout=2)` 旧线程，确保 `start_watchdog` 检查时旧线程必死，杜绝双线程/旧参数复跑。

### #7 订阅操作入锁（P2）

文件：`app/gateway/subscription.py`

- `start_snapshot_subscription` 与 `stop_subscription` 的 SDK 操作包 `_sdk_lock()`。
- 调度器 tick 遇查询持锁最多等 30s 后抛 `GatewayQueryError`，被 `_loop` 的 except 吞掉、下分钟重试——可接受。
- 回调线程（sub.run）不持 `_sdk_lock`，无死锁路径。scheduler `_start_subscription` 内 refresh_calendar/stop/start 三次串行获取 `_sdk_lock`，无嵌套。
- **shutdown 竞态明示**：lifespan shutdown 的 `scheduler._thread.join(timeout=5)` 期间若 scheduler tick 正持 `_sdk_lock`（如 refresh_calendar 阻塞），5s 超时后 shutdown 继续调 `gateway.stop_subscription()` 再等最多 30s——shutdown 最坏延迟 ~35s，可接受（Docker stop 默认 grace 10s 会 SIGKILL，亦无状态损坏，仅少一次优雅登出）。
- 锁持有验证的测试方案：测试中预持有 `gateway._lock`（真实 `AmazingDataGateway` 实例 + mock SDK import），调 `stop_subscription` 断言抛 `GatewayQueryError`（锁竞争超时）即证明其经过 `_sdk_lock`。

### #10 配置容错（P2）

文件：`app/config.py`

- 新增模块级 `_env_int(name, default)`：解析失败 warning 日志（含变量名与非法值）+ 回退默认值。覆盖全部 int 字段（port/http_port/sdk_max_concurrent/stale_threshold_sec/watchdog_interval_sec/reconnect_max_interval_sec/stale_max_age_sec/etf_flow_cache_ttl_sec）。

### #12 端点样板重构（P2）

文件：`app/http_app.py`

- 抽 `async def _run_sdk_endpoint(app, fn, *args, **kwargs)` 私有助手：承载 sdk_gate try_acquire/release + `asyncio.to_thread` + 五段异常映射（ValueError→422 / GatewayNotReadyError→503 / GatewayQueryError→502 / TypeError·OverflowError→502或500 / Exception→500）。
- **职责边界**：helper 只做 gate + 线程池 + 异常映射；各端点的请求日志（参数不同、格式不同）保留在端点函数内；`/minute` 的 period 白名单校验在调用 helper 之前执行（无效 period 不占 gate 槽位）。`/realtime` 不在重构范围（异常语义不同：fallback 失败返回 200 空数据）。
- 四个端点（/daily /minute /adj_factor /etf/net_inflow）各缩至参数解析 + 日志 + 一行调用。
- 等价重构：错误码、状态码、日志字段零变化。

### #13 死代码 + calendar set（P2）

文件：`app/http_app.py`、`app/errors.py`、`app/gateway/session.py`、`app/gateway/base.py`（Protocol 声明在 base.py）、`app/subscription_scheduler.py`、`app/health.py`、`tests/conftest.py`、`tests/test_scheduler_calendar_refresh.py`

- 删除 `http_app.py` 未使用的 `REALTIME_SUBSCRIPTION_FAILED` 导入、`errors.py` 未使用的 `Optional` 导入。
- `SessionMixin` 维护 `_calendar_set: frozenset`（login/refresh_calendar 赋值、`_safe_logout(clear_calendar=True)` 时清空），暴露 `calendar_set` 属性；`Gateway` Protocol（base.py）同步声明。
- **`@runtime_checkable` Protocol 影响面**：`tests/conftest.py` 的 `FakeGateway` 与 `tests/test_scheduler_calendar_refresh.py` 的 `FakeGW` 必须同步添加 `calendar_set` property，否则 `test_gateway_interface.py` 的 `isinstance(gw, Gateway)` 运行时检查失败。
- scheduler/health 的窗口判定改传 set。`is_subscription_window` 签名不变（`in` 对 list/set 语义一致）。`etf_flow_service` 内部的 `set(calendar)` 转换不动（YAGNI）。

## 测试计划

新增/更新用例（全部 FakeGateway）：

| 项 | 用例 |
|---|---|
| #3 | TTL 内异 codes 请求穿透查询并合并缓存；同 codes 命中不重复查询 |
| #2 | 超时后 is_ready()=False 且触发重连调度（mock _schedule_reconnect）；超时消息不含 "timed out"/"timeout"（防 `_is_connection_error` 误匹配）；**更新** `test_amazingdata_gateway.py:233-250` 两例为实例调用 |
| #4 | 第二次同参数请求零 gateway 调用；跨日 ETF 清单缓存失效 |
| #5 | tz-aware now 下 `is_subscription_window` 行为与 naive 一致（窗口内/外各一例） |
| #9 | 501 codes → 422；500 codes 正常 |
| #10 | 畸形 int env → warning + 默认值，不抛异常 |
| #12 | 四端点错误映射回归（ValueError/NotReady/QueryError/未分类异常 各 422/503/502/500） |
| #1 | `test_config.py` sdk_max_concurrent 默认值断言更新为 2 |
| #6 | stop 后旧 watchdog 线程不再存活（join 生效） |
| #7 | 预持 `gateway._lock` 后调 `stop_subscription` 断言 `GatewayQueryError`（证明经 `_sdk_lock`） |
| #13 | FakeGateway/FakeGW 加 `calendar_set` 后 `isinstance(gw, Gateway)` 通过；`calendar_set` 在 login/refresh/logout 后正确更新 |

回归：现有 107 用例全绿（含上述两处主动更新）。

## 风险与回滚

- 最大风险项是 #12（http_app 大改）与 #2（重连行为变化）；均有测试覆盖，回滚按 git 单文件还原。
- #9 的 MAX_CODES=500 若主项目存在超 500 codes 的调用会 422——已在错误消息中提示分批；属有意为之的契约收紧。

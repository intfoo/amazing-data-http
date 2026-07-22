# 日志打印优化设计

**日期**：2026-07-22
**状态**：已评审，待实现
**范围**：`app/` 服务运行日志（`scripts/` 诊断脚本与已中文的交互向导不在内）

## 1. 背景与目标

当前 `app/` 服务日志约 60 条**全英文**，存在三类问题：

1. **格式冗余**：`[%(name)s]` 输出 `[amazingdata.http]`，`amazingdata.` 前缀每行重复。
2. **措辞不统一**：`xxx failed: %s: %s` 重复 8+ 处；relogin 三连（连接错误→重连后成功/失败）在 query_snapshot / query_kline / get_adj_factor 各一套；4 路由请求日志格式基本一致但偶有差异。
3. **可读性**：业务事件（登录/订阅/查询成败）全英文，运维扫日志不直观。

**目标**（用户选定 A+B+D+C轻量）：

- **A 格式微调**：去掉 `[amazingdata.http]` 的 `amazingdata.` 冗余前缀，仅显示子模块名。
- **B 措辞统一**：统一失败模式、relogin 三连模板。
- **D 英文换中文**：业务事件词中文化，技术标识保留英文（grep 友好），"能换就换，不强求"。
- **C 轻量降噪**：两处低价值 info 降 debug；每请求耗时日志保持 info（C-keep）。
- **tgw 原生日志整体保留英文不动**（SDK 透传内容，中文化割裂）。

## 2. 现状

### 2.1 日志配置（`app/http_app.py:38-81`）

```python
_ad_root = logging.getLogger("amazingdata")
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_ad_root.addHandler(_h)
_ad_root.setLevel(logging.INFO)
_ad_root.propagate = False
```

各模块 logger：`amazingdata.http` / `.gateway` / `.realtime` / `.adj_factor` / `.kline`。
uvicorn access/error logger 也对齐加时间戳（`_align_uvicorn_log_format`），本次不动。

### 2.2 日志分布

| 模块 | logger 名 | 日志条数 |
|------|-----------|---------|
| http_app.py | amazingdata.http | 20 |
| realtime_service.py | amazingdata.realtime | 9 |
| gateway.py | amazingdata.gateway | 33（含 tgw 8 条不改） |
| kline_service.py | amazingdata.kline | 2 |
| adj_factor_service.py | amazingdata.adj_factor | 1 |
| health.py / errors.py / auth.py / serializer.py / config.py / subscription_schedule.py | — | 0 |

## 3. 设计决策

### 3.1 格式规范（A）

新增 `_ShortNameFormatter`，取 logger 名末段作 `short_name`，格式串改用 `%(short_name)s`：

```python
class _ShortNameFormatter(logging.Formatter):
    def format(self, record):
        record.short_name = record.name.split(".")[-1]
        return super().format(record)

# fmt: "%(asctime)s %(levelname)-8s [%(short_name)s] %(message)s"
```

效果：`[amazingdata.http]` → `[http]`，`[amazingdata.gateway]` → `[gateway]`。
logger 名本身不变（命名空间隔离 + `propagate=False` 仍生效），仅显示层缩短。

### 3.2 中文化边界（D）

| 中文化 | 保留英文 |
|--------|---------|
| 业务事件词：成功/失败/启动/停止/恢复/失活/降级/超时/断连/异常 | `request_id=`、错误码 `INVALID_REQUEST` 等 |
| 状态描述：非订阅时段/盘中/配置不完整/缓存为空 | SDK 方法名 `get_code_list`/`query_kline`/`query_snapshot`/`get_adj_factor` |
| 量词：只/条/个 | `type(e).__name__`（异常类名） |
| | 参数名 `codes`/`period`/`records`/`date`/`begin`/`end` |
| | 耗时字段 `gateway=%.3fs`/`flatten`/`total`/`sdk`/`lock_wait` |
| | 组件前缀 `tgw`、术语 `fallback` |

原则：技术标识是 `docker logs | grep` 的高频检索词，中文化增加排查成本，保留。

### 3.3 措辞统一规则（B）

- **失败统一**：`<方法名> 失败: %s: %s`（方法名英文 + 异常类型 + 消息）
- **relogin 三连统一**：
  - `<方法名> 连接错误，尝试重连: %s`
  - `<方法名> 重连后成功`
  - `<方法名> 重连后仍失败: %s: %s`
- **4 路由请求日志**：保留现状格式（已基本统一，`/realtime` 的 `codes=%s` 合理）

### 3.4 降噪（C 轻量）

| 日志 | 处理 |
|------|------|
| `get_code_list(...) calling SDK (lock_wait=...)` | **降 debug**（返回行保留 info） |
| `minute default range: ...` | **降 debug** |
| `kline query: gateway=%.3fs ...` | 保留 info（C-keep） |
| `adj_factor query: gateway=%.3fs ...` | 保留 info（C-keep） |
| 4 路由请求日志 | 保留 info（核心可观测性） |
| relogin 成功 / fallback 并发提示 | 保留 info（低频重要事件） |

### 3.5 tgw 日志

整体保留英文不动（`tgw FATAL:` / `tgw disconnect:` / `tgw error:` / `tgw [%s] %s` / `tgw event:` / `tgw logon event` / `tgw event logger installed...` / `tgw.g_spi not found...`）。msg 内容来自 SDK 透传，中文化割裂可读性。

## 4. 完整 before → after 清单

### 4.1 http_app.py（amazingdata.http）

| # | before | after |
|---|--------|-------|
| 1 | `auth config invalid: %s` | `认证配置无效: %s` |
| 2 | `gateway login succeeded on startup` | `启动登录成功` |
| 3 | `realtime subscription started: %d symbols (get_realtime_code_list=%.3fs subscribe=%.3fs)` | `实时订阅已启动: %d 只 (get_realtime_code_list=%.3fs subscribe=%.3fs)` |
| 4 | `realtime subscription start failed: %s: %s` | `实时订阅启动失败: %s: %s` |
| 5 | `outside subscription window, skipping subscription (SDK query still available)` | `非订阅时段，跳过订阅（SDK 查询接口仍可用）` |
| 6 | `gateway login failed on startup: %s: %s` | `启动登录失败: %s: %s` |
| 7 | `config incomplete, skipping startup login` | `配置不完整，跳过启动登录` |
| 8 | `stop subscription on shutdown: %s: %s` | `关闭时停止订阅异常: %s: %s` |
| 9 | `gateway logout on shutdown` | `关闭时已登出` |
| 10 | `gateway logout failed on shutdown: %s: %s` | `关闭时登出失败: %s: %s` |
| 11 | `request_id=%s /daily codes=%d %s..%s` | 不变 |
| 12 | `request_id=%s /minute codes=%d period=%s %s..%s` | 不变 |
| 13 | `request_id=%s /adj_factor codes=%d %s..%s` | 不变 |
| 14 | `request_id=%s /realtime codes=%s` | 不变 |
| 15 | `unhandled error: %s: %s`（×3 路由） | `未处理异常: %s: %s` |
| 16 | `request_id=%s realtime fallback: SDK not ready` | `request_id=%s realtime fallback: SDK 未就绪` |
| 17 | `request_id=%s realtime fallback failed: %s: %s` | `request_id=%s realtime fallback 失败: %s: %s` |
| 18 | `request_id=%s realtime fallback: %d codes -> %d records` | `request_id=%s realtime fallback: %d 代码 -> %d 条` |
| 19 | `request_id=%s code=%s msg=%s`（AppError handler） | 不变 |
| 20 | `request_id=%s validation failed: errors=%s body=%s` | `request_id=%s 请求校验失败: errors=%s body=%s` |

### 4.2 realtime_service.py（amazingdata.realtime）

| # | before | after |
|---|--------|-------|
| 21 | `subscription recovered: data received, reactivating` | `订阅已恢复：收到数据，重新激活` |
| 22 | `on_snapshot convert failed: %s: %s` | `快照转换失败: %s: %s` |
| 23 | `realtime subscription deactivated due to error: %s` | `实时订阅因错误停用: %s` |
| 24 | `subscription started but no data received for %.0fs, marking inactive` | `订阅启动后 %.0fs 未收到数据，标记失活` |
| 25 | `subscription stale: no data for %.0fs during trading hours, marking inactive` | `订阅失活：盘中 %.0fs 无数据` |
| 26 | `fallback query in progress, returning stale cache: %d records` | `fallback 查询进行中，返回旧缓存: %d 条` |
| 27 | `fallback query in progress, cache empty, returning []` | `fallback 查询进行中，缓存为空，返回 []` |
| 28 | `fallback query_snapshot failed: %s: %s` | `fallback query_snapshot 失败: %s: %s` |
| 29 | `fallback query_snapshot: %d records cached` | `fallback query_snapshot 已缓存: %d 条` |

### 4.3 gateway.py（amazingdata.gateway）

| # | before | after |
|---|--------|-------|
| 30 | `ADJ_FACTOR_LOCAL_PATH not configured, using default: %s. Set ADJ_FACTOR_LOCAL_PATH to a persistent absolute path for SDK caching.` | `ADJ_FACTOR_LOCAL_PATH 未配置，使用默认路径: %s（建议设为持久化绝对路径以供 SDK 缓存）` |
| 31 | `AmazingData import failed: %s` | `AmazingData SDK 导入失败: %s` |
| 32 | `AmazingData gateway login successful` | `SDK 登录成功` |
| 33 | `AmazingData login failed: %s: %s` | `SDK 登录失败: %s: %s` |
| 34 | `tgw.g_spi not found, cannot install event logger` | 不变（tgw） |
| 35 | `tgw FATAL: [%s] %s (process may exit)` | 不变（tgw） |
| 36 | `tgw disconnect: [%s] %s` | 不变（tgw） |
| 37 | `tgw error: [%s] %s` | 不变（tgw） |
| 38 | `tgw [%s] %s` | 不变（tgw） |
| 39 | `tgw event: level=%s code=%s msg=%s` | 不变（tgw） |
| 40 | `tgw logon event:%s` | 不变（tgw） |
| 41 | `tgw event logger installed on OnLog + OnEvent + OnLogon` | 不变（tgw） |
| 42 | `logout error (ignored): %s: %s` | `登出异常（已忽略）: %s: %s` |
| 43 | `get_code_list(security_type=%s) calling SDK (lock_wait=%.3fs)` | **降 debug** + `get_code_list(security_type=%s) 调用 SDK (lock_wait=%.3fs)` |
| 44 | `get_code_list failed: %s: %s` | `get_code_list 失败: %s: %s` |
| 45 | `get_code_list(security_type=%s) returned %d codes (sdk=%.3fs total=%.3fs)` | `get_code_list(security_type=%s) 返回 %d 个代码 (sdk=%.3fs total=%.3fs)` |
| 46 | `get_code_list(EXTRA_INDEX_A) failed, degrading to stock-only: %s: %s` | `get_code_list(EXTRA_INDEX_A) 失败，降级为仅股票: %s: %s` |
| 47 | `get_realtime_code_list done: %d stocks in %.3fs (index failed, total %.3fs)` | `实时代码列表就绪: %d 只股票 (%.3fs，指数失败，总计 %.3fs)` |
| 48 | `get_realtime_code_list: %d stocks + %d indices = %d total (stock=%.3fs index=%.3fs total=%.3fs)` | `实时代码列表: %d 股票 + %d 指数 = %d (stock=%.3fs index=%.3fs total=%.3fs)` |
| 49 | `query_snapshot failed: %s: %s (codes=%d, date=%s)` | `query_snapshot 失败: %s: %s (codes=%d, date=%s)` |
| 50 | `query_snapshot connection error, attempting relogin: %s` | `query_snapshot 连接错误，尝试重连: %s` |
| 51 | `query_snapshot succeeded after relogin` | `query_snapshot 重连后成功` |
| 52 | `query_snapshot failed after reconnect: %s: %s` | `query_snapshot 重连后仍失败: %s: %s` |
| 53 | `query_kline failed: %s: %s (codes=%d, begin=%s, end=%s, period=%s)` | `query_kline 失败: %s: %s (codes=%d, begin=%s, end=%s, period=%s)` |
| 54 | `query_kline connection error, attempting relogin: %s` | `query_kline 连接错误，尝试重连: %s` |
| 55 | `query_kline succeeded after relogin` | `query_kline 重连后成功` |
| 56 | `query_kline failed after reconnect: %s: %s` | `query_kline 重连后仍失败: %s: %s` |
| 57 | `snapshot callback error: %s: %s` | `快照回调异常: %s: %s` |
| 58 | `SubscribeData.run() returned unexpectedly — session may have been kicked` | `SubscribeData.run() 异常返回（会话可能被踢）` |
| 59 | `subscription thread crashed: %s: %s` | `订阅线程崩溃: %s: %s` |
| 60 | `snapshot subscription started: %d symbols` | `快照订阅已启动: %d 只` |
| 61 | `stop subscription error (ignored): %s: %s` | `停止订阅异常（已忽略）: %s: %s` |
| 62 | `get_adj_factor failed: %s: %s (codes=%d)` | `get_adj_factor 失败: %s: %s (codes=%d)` |
| 63 | `get_adj_factor connection error, attempting relogin: %s` | `get_adj_factor 连接错误，尝试重连: %s` |
| 64 | `get_adj_factor succeeded after relogin` | `get_adj_factor 重连后成功` |
| 65 | `get_adj_factor failed after reconnect: %s: %s` | `get_adj_factor 重连后仍失败: %s: %s` |

### 4.4 kline_service.py（amazingdata.kline）

| # | before | after |
|---|--------|-------|
| 66 | `minute default range: begin_date=%s (last 365 days)` | **降 debug** + `minute 默认范围: begin_date=%s (近 365 天)` |
| 67 | `kline query: gateway=%.3fs flatten=%.3fs period=%s codes=%d records=%d` | 不变（C-keep） |

### 4.5 adj_factor_service.py（amazingdata.adj_factor）

| # | before | after |
|---|--------|-------|
| 68 | `adj_factor query: gateway=%.3fs process=%.3fs codes=%d records=%d` | 不变（C-keep） |

### 4.6 格式层

| # | before | after |
|---|--------|-------|
| 69 | `[%(name)s]` → `[amazingdata.http]` | `_ShortNameFormatter` + `[%(short_name)s]` → `[http]` |

## 5. 不改动项

- `scripts/run.py`（已中文）、`scripts/probe_*.py`（诊断脚本，非服务运行日志）
- `health.py` / `errors.py` / `auth.py` / `serializer.py` / `config.py` / `subscription_schedule.py`（无 logger 调用）
- uvicorn access/error 日志格式（`_align_uvicorn_log_format` 不动）
- 错误响应体 `message` 字段（API 契约，如 `"请求体校验失败"` 已中文的不动）
- tgw 全部日志（§3.5）
- 日志级别（除 #43 #66 降 debug 外，其余不变）

## 6. 验证策略

- **单元测试**：`tests/` 无 logger 文本断言，改消息不破坏测试。`pytest` 全套件应仍全绿（107 passed）。
- **格式验证**：本地起服务，确认启动日志前缀从 `[amazingdata.http]` 变为 `[http]`，中文化消息可读。
- **grep 验证**：`docker logs amazing-data-http | grep "request_id="` / `grep "query_kline"` / `grep "gateway="` 仍可命中（技术标识保留英文）。
- **降噪验证**：默认 INFO 级下 `get_code_list calling SDK` 与 `minute default range` 不再出现；开 DEBUG 级重现。

## 7. 风险与回滚

- **风险低**：纯日志文本与格式改动，无业务逻辑/控制流变更。无日志解析系统消费这些文本（`docker logs` 人工阅读为主）。
- **回滚**：若中文化影响外部日志采集，git revert 单次提交即可恢复英文。

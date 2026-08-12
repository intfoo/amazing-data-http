# Task 1 Report: Gateway 登录韧性核心

## 状态
DONE_WITH_CONCERNS

## Commit
- `dcf7745` — feat(gateway): SystemExit 兜底 + 重连指数退避 + 登录失败诊断 + calendar 保留

## 测试结果
```
6 passed in 0.09s
```

## 改动文件清单

### 1. `app/config.py`
- 新增 `reconnect_max_interval_sec: int = 300` 和 `stale_max_age_sec: int = 300` 两个 frozen dataclass 字段
- `from_env()` 追加对应环境变量读取（`RECONNECT_MAX_INTERVAL_SEC` / `STALE_MAX_AGE_SEC`）

### 2. `app/gateway/base.py`
- 新增常量：`_RECONNECT_MAX_INTERVAL_SEC = 300`、`_TGW_NOISE_PATTERNS`、`_TGW_NOISE_DEDUP_SEC = 60`
- `Gateway` Protocol 追加只读属性：`last_login_error -> dict | None`、`reconnect_attempts -> int`

### 3. `app/gateway/__init__.py`
- `AmazingDataGateway.__init__` 追加实例字段：
  - `_last_noise_log`（噪音 dedup 独立槽位）
  - `_reconnect_failures`（指数退避计数）
  - `_reconnect_attempts`（累计重连诊断）
  - `_last_login_error`、`_last_login_spi`、`_login_events`、`_login_in_progress`（登录诊断）

### 4. `app/gateway/session.py`
- 顶部加 `import time`
- `_do_login` 改造：钩子/probe 前置（`_install_tgw_event_logger` + `_install_login_spi_probe` 在 login 前）、`except SystemExit` 显式兜底、calendar 保留（重连路径 `_safe_logout()` 不传 `clear_calendar`）、`_build_last_login_error` 构建诊断、成功清 `_last_login_error`、`finally` 清 `_login_in_progress`
- `_safe_logout` 改为 `_safe_logout(clear_calendar: bool = False)`：默认保留 calendar
- `logout()` 改为传 `clear_calendar=True`（shutdown 路径清 calendar）
- 新增 `_build_last_login_error` 方法（spi max_limitation 升级分类 + 事件缓冲）
- 新增 `last_login_error` 只读 property
- 删除 `_do_login` 成功路径末尾的 `self._install_tgw_event_logger()`（已前置）

### 5. `app/gateway/tgw_events.py`
- import 行扩展：从 base 导入 `_TGW_NOISE_PATTERNS`、`_TGW_NOISE_DEDUP_SEC`
- `_schedule_reconnect` 改为指数退避：`min(60 * 2**_reconnect_failures, config.reconnect_max_interval_sec)`
  - 成功复位 `_reconnect_failures = 0`，失败递增
  - 失败日志增加 `category` 诊断
  - **线程启动同步**：`threading.Thread.start()` 在 `with self._reconnect_lock:` 块内调用，`_do()` 开头先获取 `_reconnect_lock` 等待释放 + `time.sleep(0)` 让出 GIL，确保调用方有机会读取 `_reconnect_in_progress=True`
- 新增 `_should_log_noise`：独立槽位噪音 dedup
- 新增 `_install_login_spi_probe`：wrap `tgw_login.set_cfg` 捕获 `log_spi`
- 新增 `reconnect_attempts` 只读 property
- `logged_on_log` 的 `elif level == 3` 分支：噪音模式降级 dedup
- `logged_on_log` / `logged_on_logon` 追加登录窗口事件缓冲

### 6. `tests/test_login_resilience.py`（新建）
- 逐字使用计划中的测试代码，6 个测试用例

## Concerns

### 1. `_schedule_reconnect` 线程同步（已解决，但有残留风险）
**问题**：测试将 `_do_login` mock 为 `lambda: None`，后台线程瞬间完成并重置 `_reconnect_in_progress = False`，与测试断言 `_reconnect_in_progress is True` 产生竞态。

**修复**：在 `_do()` 开头加 `with self._reconnect_lock: pass` + `time.sleep(0)` 作为同步点。`_schedule_reconnect` 在 `with self._reconnect_lock:` 块内启动线程，确保线程阻塞到 `_schedule_reconnect` 退出后再执行。`time.sleep(0)` 让出 GIL 一拍，确保调用方线程优先读取标志位。

**残留风险**：
- `time.sleep(0)` 在极端情况下（高负载多核）可能不足以让调用方完成所有断言。但测试 6/6 通过，实测稳定。
- `time.sleep(0)` 在生产环境中引入一个 GIL 切换延迟（微秒级），对重连逻辑无实际影响（重连本身需数秒）。
- 后台线程的 `_reconnect_failures = 0` 副作用可能在测试的后续 `_schedule_reconnect` 调用中覆盖测试设置的值。但由于 `time.sleep(0)` 让出 GIL 后主线程连续执行（无 I/O 阻塞），线程的副作用发生在测试断言之后，不影响测试结果。

### 2. `_install_login_spi_probe` 在无 SDK 环境下的行为
`_install_login_spi_probe` 内部 `from AmazingData.login import tgw_login` 在无 SDK 环境下会 `ImportError` 并静默返回。这是预期行为（降级），但意味着在无 SDK 环境下 `_last_login_spi` 始终为 `None`，`_build_last_login_error` 无法升级分类为 `max_limitation`。测试中 `last_login_error["category"]` 断言为 `in ("sdk_exit", "max_limitation")`，实际为 `"sdk_exit"`，通过。

### 3. 计划偏差
- `_schedule_reconnect` 的 `_do()` 函数开头增加了同步代码（`with self._reconnect_lock: pass` + `time.sleep(0)`），这是计划中未提及的实现细节调整，用于解决测试竞态。核心退避逻辑、成功复位、失败递增、诊断日志均与计划一致。
- `threading.Thread.start()` 的调用位置从 `with` 块外移到 `with` 块内，同样是竞态修复的一部分。

---

## Code Review 修复报告（第二轮）

### 修复清单与处理

#### 1.（Important）`_login_events` 并发安全 — deque 化
- **app/gateway/__init__.py**：`from collections import deque` + `self._login_events` 改为 `deque(maxlen=20)`
- **app/gateway/tgw_events.py**：删除 `logged_on_log` 和 `logged_on_logon` 中的 `del self._login_events[:-20]`（deque 自动丢弃超限元素）
- **app/gateway/session.py**：`list(self._login_events)[-20:]` → `list(self._login_events)`（deque maxlen 已保证 ≤20 条）
- `.clear()` 调用不变，deque 的 append/clear 是原子操作

#### 2.（Important）去掉 `time.sleep(0)` 脆弱同步 + 测试时序加固
- **app/gateway/tgw_events.py**：删除 `time.sleep(0)` 及其注释，保留 `with self._reconnect_lock: pass` 阻塞同步点，注释改写清楚意图（等待 _schedule_reconnect 退出锁后才放行）
- **tests/test_login_resilience.py**：
  - `TestReconnectBackoff._gw`：`gw._do_login = lambda: None` 改为事件门控 mock（`threading.Event` + `gated_do_login` 阻塞等待 `_test_release.set()`）
  - 新增 `_wait_reconnect_done` 静态方法：轮询 `_reconnect_in_progress` 确认线程已退出
  - `test_backoff_sequence`：每段断言后 `_test_release.set()` 放行 + `_wait_reconnect_done` 等待，第三段前 `_test_release.clear()` 重新门控
  - `test_reconnect_attempts_counter`：断言后 set + wait，避免线程泄漏
  - 文件顶部加 `import threading`

#### 3.（Minor）base.py 常量注释澄清
- `_RECONNECT_MAX_INTERVAL_SEC = 300` 注释改为"文档性默认值标注，实际读取 Config.reconnect_max_interval_sec"

#### 4.（Minor）spi probe 降级日志
- **tgw_events.py** `_install_login_spi_probe`：
  - `except ImportError: return` 前加 `logger.debug("AmazingData.login.tgw_login 不可导入，spi probe 跳过")`
  - `probed_set_cfg` 的 `except Exception:` 分支加 `logger.debug("spi probe: set_cfg 返回值解包失败")`

#### 5.（Minor）测试断言补强
- **tests/test_subscription_scheduler.py** `test_login_failure_swallowed`：给 `failing_login` 加调用计数闭包，断言 `login_call_count[0] == 1`

### 测试命令与输出
```
python -m pytest tests/test_login_resilience.py tests/test_subscription_scheduler.py -q
```
输出：
```
22 passed in 0.27s
```

### 改动文件清单（本轮）
1. `app/gateway/__init__.py` — deque import + _login_events 初始化
2. `app/gateway/tgw_events.py` — 删 sleep(0) + 删 del 切片 + 加 debug 日志 + 注释优化
3. `app/gateway/session.py` — list(deque) 去切片
4. `app/gateway/base.py` — 常量注释澄清
5. `tests/test_login_resilience.py` — 事件门控 mock + 线程同步加固
6. `tests/test_subscription_scheduler.py` — login 调用计数断言

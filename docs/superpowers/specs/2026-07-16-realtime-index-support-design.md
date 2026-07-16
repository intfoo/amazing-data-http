# 实时接口支持指数数据 — 设计文档

- 日期：2026-07-16
- 状态：已验证（probe 通过）
- 关联 probe：`docs/probe-index-mixed.json`、`scripts/probe_index_mixed.py`

## 1. 背景与目标

现有 `GET /realtime` 只订阅 A 股 level-1 快照（`EXTRA_STOCK_A`，~5529 只），通过单 `SubscribeData` 实例订阅 → 内存缓存 → HTTP 读缓存，缓存空时 fallback 到 `query_snapshot` 查当日历史快照。

目标：让 `/realtime` 同时返回**指数**实时数据，与股票混合在同一份响应里。性能优先——不新增 SDK 连接、不新增订阅线程。

## 2. 关键约束

- **SDK 连接数有上限**：`gateway.py` 注释记录过 TGW 报 `"Connections of this user exceed the max limitation"`。订阅实例数 = 连接数，必须最小化。
- **现有哲学**：适配服务透传 SDK 原始字段，不重命名/不换算/不衍生，字段映射由主项目 `field_map` 完成。指数数据沿用此哲学，不额外加 `type` 字段。
- **订阅推送只在交易时段生效**：非交易时段缓存为空，自动 fallback 到 `query_snapshot`。

## 3. Probe 验证结论（2026-07-16 17:43，非交易时段）

用 `scripts/probe_index_mixed.py` 验证方案 A 核心假设，结果写入 `docs/probe-index-mixed.json`：

| 验证项 | 结果 | 细节 |
|---|---|---|
| `query_snapshot` 混合 code_list | ✅ accepted | 股票+指数 4 代码同一次查询全返回 |
| `SubscribeData.register` 混合 code_list | ✅ register_succeeded | 装饰器无异常，`Period.snapshot.value=10014` |
| 盘中回调类型混合 | ⏠ 待交易时段 | 非交易时段 `callbacks=0`（预期） |

### 3.1 实测 Schema（手册有出入，以实测为准）

**股票 `Snapshot`（35 列）**：`code, trade_time, pre_close, last, open, high, low, close, volume, amount, num_trades, high_limited, low_limited, ask_price1~5, ask_volume1~5, bid_price1~5, bid_volume1~5, iopv, trading_phase_code`

**指数 `SnapshotIndex`（11 列）**：`code, trade_time, last, pre_close, high, open, low, close, volume, amount, trading_phase_code`

> 手册 §4.2.2 称 `SnapshotIndex` 为 10 列（无 `trading_phase_code`），实测多出 `trading_phase_code`，共 11 列。指数无 5 档 ask/bid、无 `num_trades`、无涨跌停价、无 `iopv`。

- 公共字段 11 个；股票多 24 个字段。混合响应里指数记录是"稀疏"的。
- 代码不冲突：`000001.SH`（上证指数）vs `000001.SZ`（平安银行）市场后缀不同，统一 `dict[code, dict]` 无 key 碰撞。
- `_snapshot_to_dict` 通用提取器（dataclass / `__dict__` / `__slots__` 三级降级）对两种对象都适用，**无需改 schema 代码**。

### 3.2 残留风险（低）

盘中回调是否同时收到 `Snapshot` + `SnapshotIndex` 未实测。风险低，依据：
1. SDK 手册类型提示 `onSnapshot(data: Union[Snapshot, SnapshotIndex])` 与 `onSnapshotindex(data: Union[Snapshot, SnapshotIndex])` 均允许 Union，暗示 SDK 按 code 类型路由、两种对象都会投递。
2. `query_snapshot` 同源路由已验证混合正常。
3. `_snapshot_to_dict` 对任意对象类型都容错。

Follow-up：交易日 9:30-11:30 或 13:00-15:00 跑 `python scripts/probe_index_mixed.py --sub-timeout 60`，确认 `callback_by_type` 同时含两类。

## 4. 方案选型

三个候选方案（订阅拓扑）：

| 方案 | 订阅实例 | 连接 | 线程 | 评估 |
|---|---|---|---|---|
| **A（选定）** 单实例合并 code_list | 1 | 1 | 1 | 连接最少，probe 已验证 register 接受混合 list |
| B 两实例两线程 | 2 | 2 | 2 | 踩连接上限风险最高，淘汰 |
| C 单实例双 register | 1 | 1 | 1 | SDK 是否支持单实例多 register 未验证，A 已够用 |

**选定 A**：probe 证明 `SubscribeData.register(code_list=[股票+指数], period=Period.snapshot.value)` 装饰器应用无异常。1 连接 1 线程，规避连接上限，零结构性成本。

## 5. 架构

```
启动 (后台线程 _init_subscription)
  get_code_list(EXTRA_STOCK_A)  ─┐
  get_code_list(EXTRA_INDEX_A)  ├─ 合并 code_list（缓存到 service 供 fallback 复用）
                                 │
  SubscribeData.register(合并 list, Period.snapshot.value)  ← 单实例/单连接/单线程
  sub.run()  ──回调──→ on_snapshot ──→ _cache[code] = dict  (股票 35 字段 / 指数 11 字段)

GET /realtime?codes=...
  snapshot(codes)  → 读 _cache 过滤返回（混合，schema 稀疏）
  缓存空 → fallback_snapshot  → query_snapshot(合并 list) tail(1)，60s TTL
```

## 6. 组件改动

### 6.1 `app/gateway.py`（小改）

- `start_snapshot_subscription(code_list, ...)`：签名不变，已接受任意 code_list。
- `get_code_list(security_type)`：已支持 `"EXTRA_INDEX_A"`，无需改。
- `query_snapshot(codes, ...)`：已接受混合 list（probe 验证），无需改。
- **新增** `get_realtime_code_list() -> list[str]`：便捷方法，返回 `get_code_list("EXTRA_STOCK_A") + get_code_list("EXTRA_INDEX_A")`，供 startup 和 fallback 复用。指数列表获取失败时降级为只返回股票列表（日志 warn，不抛）。**必须加入 `Gateway` Protocol 定义**（`app/gateway.py:42-63`），保证 `AmazingDataGateway` 与 `FakeGateway` 接口一致。

### 6.2 `app/realtime_service.py`（小改）

- `_snapshot_to_dict`：**不改**（通用提取器，probe 验证对两种对象生效）。
- `fallback_snapshot`：`codes=None` 分支把 `self._gw.get_code_list()`（纯股票）改为优先用 `self._combined_code_list`，否则调 `self._gw.get_realtime_code_list()`（股票+指数）。**注意**：`codes` 参数非空时直接用用户传入的 codes 查询（现有逻辑不变，不调 `get_realtime_code_list`），`set_combined_code_list` 缓存仅优化 `codes=None` 路径。伪代码：
  ```python
  # fallback_snapshot 中 codes=None 分支：
  if self._combined_code_list is not None:
      code_list = self._combined_code_list
  else:
      code_list = self._gw.get_realtime_code_list()
  ```
- **新增** `_combined_code_list` 缓存字段：startup 时由 `set_combined_code_list` 注入，`fallback_snapshot` 优先用缓存列表，避免每次 TTL 过期重查 `get_code_list`。缓存未设置时回退到实时调 `get_realtime_code_list()`（保持向后兼容）。

### 6.3 `app/http_app.py`（小改）

- `_init_subscription`：调 `gateway.get_realtime_code_list()` **一次**获取合并列表（内部已处理指数降级：指数列表失败则只返回股票），传给 `start_snapshot_subscription` 和 `realtime_service.set_combined_code_list(合并 list)`。降级逻辑集中在 `get_realtime_code_list()` 内部，`_init_subscription` 不再重复 catch。
- 启动日志补 index code 数量。

### 6.4 `tests/conftest.py`（FakeGateway 扩展）

- `FakeGateway.get_code_list(security_type)`：支持 `EXTRA_INDEX_A` 返回指数代码列表（如 `["000001.SH", "399001.SZ"]`）。
- `FakeGateway` 补 `get_realtime_code_list()`（已加入 `Gateway` Protocol，见 §6.1）。

## 7. 数据流

### 7.1 盘中（订阅活跃）

SDK 推送 `Snapshot`（股票）/ `SnapshotIndex`（指数）→ `on_snapshot` 通用转 dict → `_cache[code]` 覆盖最新一笔。`/realtime` 读缓存，混合返回。指数记录 11 字段、股票记录 35 字段，消费方按 code 自行处理稀疏字段。

### 7.2 非交易时段 / 订阅未就绪（fallback）

`codes=None` 时 `query_snapshot(合并 list, 当日)` 一次返回股+指；`codes` 非空时 `query_snapshot(用户 codes, 当日)`（现有逻辑不变）。→ 每 code 取 `tail(1)` → 60s TTL 缓存。`codes` 过滤在缓存结果上应用。query_snapshot 失败返回空列表（不抛，让 `/realtime` 返回空）。

## 8. 性能特性

| 维度 | 现状（纯股票） | 加指数后 | 评估 |
|---|---|---|---|
| SDK 连接数 | 1 | 1 | 不变，规避连接上限 |
| 订阅 code 数 | ~5529 | ~5529 + 指数(百~千级) | 回调量略增，单线程串行处理 |
| 缓存内存 | ~5529 dict × 35 字段 | +指数 dict × 11 字段 | 增量很小（指数字段少） |
| fallback 单次查询 | 全市场股票 | +指数 | 增量中等，60s TTL 兜底 |
| 启动 init 耗时 | get_code_list ×1 (~10-20s) | ×2 (~20-40s) | 后台线程不阻塞 startup，期间走 fallback |
| `/realtime` 响应延迟 | O(缓存 dict 查找) | 不变 | 读路径零额外成本 |

## 9. 错误处理

- **指数 `get_code_list` 失败**：startup 后台线程 catch，降级为只订阅股票（日志 warn），不影响服务启动。fallback 同理——指数列表拿不到就只查股票。
- **混合订阅 register 失败**（理论不会，probe 已验证）：`on_subscription_error` 标记 inactive，`/realtime` 走 fallback。
- **`query_snapshot` 部分代码无数据**：现有逻辑已处理（`df is None or df.empty` 跳过），指数代码同理。
- **盘中回调收到未知对象类型**：`_snapshot_to_dict` 三级降级提取，提取不出返回 `{}`，`on_snapshot` 跳过 code 为空的记录——已有容错。
- **`get_realtime_code_list()` 返回空列表**：股票+指数列表都为空（理论不会），`start_snapshot_subscription` 收到空 list，log "0 symbols"，订阅无效但不抛异常；`/realtime` 走 fallback 也返回空。

## 10. 测试策略

- `FakeGateway.get_code_list` 支持 `EXTRA_INDEX_A` 返回指数代码（当前实现忽略 `security_type` 参数，需改为按 type 返回不同列表）。
- 新增单测：
  - `on_snapshot` 收到指数风格 dict（11 字段）能正确缓存。
  - `snapshot()` 混合返回股票+指数。
  - `fallback_snapshot` 用 FakeGateway 验证合并 list 查询 + codes 过滤。
  - `set_combined_code_list` 注入后 fallback 复用缓存列表。
  - `http_app` 启动：`_init_subscription` 传了合并 list（通过 FakeGateway 记录调用参数）。
- **不新增集成测试**（需真实 SDK + 盘中），改为盘中 follow-up probe（§3.2）。
- **回归基线**：现有 128 个单测，其中 5 个 `test_run.py` 失败为预存问题（`build_docker_*`→`build_container_*` 命名变更未同步，与本次改动无关）。realtime 相关测试全绿，以此为回归基线。实现前先修复 `test_run.py` 或标记 xfail，确保基线干净。本次改动后 realtime 测试全绿且不引入新失败。

## 11. 不做的事（YAGNI）

- 不新增 `/realtime/index` 端点（用户选混合响应）。
- 不加 `type`/`security_type` 字段（保持透传哲学，消费方按 code 区分）。
- 不拆分股票/指数为两个订阅实例（连接上限约束）。
- 不对指数做独立 TTL 缓存（共享 fallback 60s TTL 足够）。
- 不改 `_snapshot_to_dict`（通用提取器已覆盖）。

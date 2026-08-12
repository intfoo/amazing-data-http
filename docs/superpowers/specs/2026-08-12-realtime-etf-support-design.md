# 实时行情接口添加 ETF 数据 — 设计文档

## 1. 背景与目标

当前 `GET /realtime` 的实时订阅覆盖股票（`EXTRA_STOCK_A`）+ 指数（`EXTRA_INDEX_A`），不含 ETF。
SDK 手册确认 ETF 实时快照完全可行（3.5.3.5：ETF 复用标准 `Snapshot` 结构，订阅方式与股票一致；
4.2.1：`iopv` 字段仅基金品种有效；3.5.4.1：历史快照 `query_snapshot` 支持股票/ETF/可转债混合列表）。

**目标**：

1. 实时订阅代码表合并全量 ETF（`EXTRA_ETF`，约 1000+ 只），`/realtime` 全市场响应包含 ETF。
2. `/realtime` 新增可选 `types` 参数按证券类型筛选（stock/index/etf），响应记录注入 `security_type` 字段。

**先例**：`2026-07-16-realtime-index-support-design.md`（指数支持）走同一模式，本次为其翻版 + 类型筛选。

## 2. 关键决策（已与用户确认）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | ETF 订阅范围 | **全量 ETF**（`EXTRA_ETF`）。订阅层保持"全市场"语义，筛选交给客户端 |
| 2 | 类型筛选实现 | **订阅时打标**：构建 `code → type` 映射存入 RealtimeService；响应注入 `security_type` 字段。ETF 与股票同为 `Snapshot` 结构，快照数据本身无类型字段，只能在建表时记录；拒绝代码前缀推断（规则脆弱） |
| 3 | fallback/未知代码语义 | **宽容模式**：类型在映射中且不匹配 → 排除；不在映射中的代码 → 放行。盘后零额外开销，新品种（可转债等）前向兼容 |

## 3. 架构与数据流

```
SubscriptionScheduler._start_subscription()
  gw.get_realtime_universe()  →  dict[code, "stock"|"index"|"etf"]   ← 原 get_realtime_code_list 改造
    ├─ code_list = list(universe.keys())
    │    → gw.start_snapshot_subscription(code_list, on_data, on_error)  （不变）
    └─ realtime_service.set_type_map(universe)                          （新增）

GET /realtime?codes=...&types=stock,etf
  → realtime_service.snapshot(codes, types)           订阅缓存路径：类型过滤 + security_type 注入
  → realtime_service.fallback_snapshot(codes, types)  fallback 路径：同样过滤
```

`_snapshot_to_dict` 的按类型自适应序列化（dataclass.asdict / vars / slots）无需改动，
ETF 特有字段（如 `iopv`）自然带出。

## 4. 各层改动

### 4.1 Gateway 层（`app/gateway/query_market.py`）

- `get_realtime_code_list()` **改名改造**为 `get_realtime_universe() -> dict[str, str]`：
  依次取 `EXTRA_STOCK_A` → `EXTRA_INDEX_A` → `EXTRA_ETF`，一次 SDK 调用序列同时产出代码表和类型映射。
  - 股票列表失败：异常正常传播（与现状一致）
  - 指数/ETF 列表失败：各自独立降级（warn 日志 + 跳过该类），不影响其余类别
- `Gateway` Protocol（`app/gateway/base.py`）同步替换方法签名。
- 不留 `get_realtime_code_list` 兼容层（全仓仅 `subscription_scheduler` + 1 个测试调用，直接替换）。

### 4.2 RealtimeService 层（`app/realtime_service.py`）

- 新增 `_type_map: dict[str, str]`（初始空）+ `set_type_map(mapping)`：调度器启动订阅时调用。
  `clear_cache()` 时一并清空类型映射。
- `on_snapshot`：缓存前注入 `record["security_type"] = self._type_map.get(code, "unknown")`。
- `snapshot(codes, types=None)` / `fallback_snapshot(codes, types=None)` 增加可选 `types: set[str] | None`：
  - `types is None` → 不过滤（向后兼容）
  - 记录 `security_type` 在 `types` 中 → 保留
  - 记录类型为 `"unknown"`（不在映射中）→ **放行**（宽容模式）
- 锁内只取引用快照，过滤在锁外完成（延续现有性能模式）。
- **fallback 路径的记录不经过 `on_snapshot`**，本身无 `security_type` 字段：在 `_filter_fallback`
  阶段按 `code` 查 `_type_map` 统一注入 `security_type` 并应用 `types` 过滤（fallback 缓存为全量共享，
  注入与过滤都是纯内存操作，无额外 SDK 调用；注入在过滤后/返回前的浅拷贝上进行，不污染共享缓存）。

### 4.3 HTTP 层（`app/http_app.py`）

- `/realtime` 新增可选 query 参数 `types`（逗号分隔，如 `?types=etf,index`）。
- 合法值集合 `{stock, index, etf}`；含非法值 → 422 `INVALID_REQUEST`（显式报错优于静默忽略）。
- `codes` 与 `types` 可叠加：先按 codes 过滤，再按 types 过滤。
- 不传 `types` → 行为与现状完全一致。

### 4.4 调度器（`app/subscription_scheduler.py`）

- `_start_subscription`：`code_list = self._gw.get_realtime_code_list()` 改为
  `universe = self._gw.get_realtime_universe()`；`list(universe)` 传给订阅，`universe` 传给
  `realtime_service.set_type_map()`。

## 5. 错误处理与边界

| 场景 | 行为 |
|------|------|
| ETF 列表拉取失败 | 降级为 股票+指数，warn 日志，订阅正常启动 |
| 盘后启动、映射表为空 | 所有代码视为 unknown → `types` 过滤全放行，`security_type="unknown"` |
| 同一 code 出现在多个列表 | dict 后写覆盖（股票/指数/ETF 代码段实际不重叠，理论防护） |
| 客户端传映射外代码 + types | 放行（宽容模式），如 `?codes=510300.SH&types=etf` 盘后也能拿到数据 |
| watchdog / 订阅窗口 / 健康检查 | 不变 |

## 6. 测试策略

- `tests/conftest.py` FakeGateway：`get_code_list` 支持 `EXTRA_ETF` 返回 ETF 代码表；
  `get_realtime_code_list` 替换为 `get_realtime_universe` 返回 `dict[code, type]`。
- `tests/test_realtime_service.py`：
  - `on_snapshot` 注入 `security_type`（已知类型 / unknown）
  - `snapshot(types=...)`：etf 命中保留、stock 排除、unknown 放行、`types=None` 不过滤
  - `fallback_snapshot(types=...)` 过滤一致
  - `clear_cache` 清空类型映射
- `tests/test_http_app.py`：
  - `?types=etf` 正常过滤
  - 非法 type 值 → 422 `INVALID_REQUEST`
  - `codes` + `types` 叠加
  - 不传 types 行为不变（回归）
- `tests/test_subscription_scheduler.py`：适配 `get_realtime_universe` 新签名。

## 7. 不做的事（YAGNI）

- 不做 ETF 单独端点（`/realtime/etf`）——参数过滤足够
- 不做类型映射持久化/独立缓存——随订阅生命周期重建即可
- 不改 `Snapshot` 字段集——`iopv` 等 ETF 特有字段由自适应序列化自然带出
- 不支持可转债/港股通等其他品种——按需后续扩展（宽容模式已预留兼容）
- 不做订阅范围配置化（如只订 ETF 不订股票）——无需求

## 8. 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `app/gateway/query_market.py` | 修改 | `get_realtime_code_list` → `get_realtime_universe`，加 EXTRA_ETF + 类型映射 |
| `app/gateway/base.py` | 修改 | `Gateway` Protocol 方法签名替换 |
| `app/realtime_service.py` | 修改 | `set_type_map` / `security_type` 注入 / `types` 过滤（snapshot + fallback） |
| `app/subscription_scheduler.py` | 修改 | 适配 `get_realtime_universe`，调用 `set_type_map` |
| `app/http_app.py` | 修改 | `/realtime` 新增 `types` 参数 + 422 校验 |
| `tests/conftest.py` | 修改 | FakeGateway 支持 EXTRA_ETF + `get_realtime_universe` |
| `tests/test_realtime_service.py` | 修改 | 类型注入/过滤/清空测试 |
| `tests/test_http_app.py` | 修改 | types 参数 HTTP 测试 + 适配 |
| `tests/test_subscription_scheduler.py` | 修改 | 适配新签名 |
| `docs/API.md` | 修改 | `/realtime` 章节补充 `types` 参数与 `security_type` 字段 |

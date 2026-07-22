# /adj_factor 端点设计

## 背景与目标

amazingDataHttp 是 AmazingData SDK 的 HTTP 适配器，已暴露 `/daily`、`/minute`、`/realtime`、`/health`。
外部项目（stocker / tickflow-stock-panel）的自定义数据源协议定义了 `adj_factor` 数据集，需要一个对应的 HTTP 端点获取**除权因子**。

目标：新增 `POST /adj_factor` 端点，对接 AmazingData SDK 的 `BaseData.get_adj_factor`（手册 3.5.2.6 单次复权因子），返回每次除权除息事件的单次因子。

### 选型依据

 AmazingData SDK 提供两个复权因子接口：

| 接口 | 函数 | 语义 |
|---|---|---|
| 3.5.2.5 后复权因子 | `get_backward_factor` | 累计因子（后复权价 = 原始价 × 因子） |
| 3.5.2.6 单次复权因子 | `get_adj_factor` | 每次除权事件的单次因子 |

选 **3.5.2.6 `get_adj_factor`**，理由：
- stocker 的 canonical schema 是 `symbol/trade_date/ex_factor` 长表，按 `symbol+trade_date` 去重（事件粒度，`tickflow-stock-panel/backend/app/services/kline_sync.py:356-358`）
- stocker 用除权因子做**前复权**（`fetch_adj_factor_single` 注释"用于单股 K 线即时前复权"，`kline_sync.py:683`），需要单次因子自行累乘
- 函数名 `get_adj_factor` ↔ stocker 数据集名 `adj_factor`
- 对照 stocker 原生 TickFlow 的 `tf.klines.ex_factors()`（ex-dividend 因子，单次事件语义）

## 设计原则

**接口通用，不与 stocker 耦合**（用户明确要求）。遵循现有 `/daily` 的范式：

- 参数名中性：`codes` / `start_time` / `end_time`（与 `/daily` 一致），不用 stocker 的 `symbols`
- 响应字段用 SDK/通用术语：`code` / `trade_date` / `adj_factor`，不重命名为 stocker 的 `symbol` / `ex_factor`
- 响应格式统一 `{"data": [...]}`
- 不做字段重命名、单位换算、复权计算（`kline_service.py:9` 同款原则）
- stocker 通过自身 YAML 的 `symbols_param: codes` + `field_map: {code: symbol, trade_date: trade_date, adj_factor: ex_factor}` 适配

## 接口契约

### 请求

```
POST /adj_factor
Content-Type: application/json

{
  "codes": ["000001.SZ", "600000.SH"],
  "start_time": "2024-01-01",     // 可选, ISO 日期或日期时间
  "end_time":   "2024-06-30"       // 可选, 同上
}
```

- `codes`：必填，非空股票代码列表（标准格式 `000001.SZ`）
- `start_time` / `end_time`：可选，ISO 格式（`YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SS`），未传时该侧不过滤
- 两者都传时校验 `start_time <= end_time`，失败返回 422

### 响应

```json
{
  "data": [
    {"code": "000001.SZ", "trade_date": "2024-05-30", "adj_factor": 1.05},
    {"code": "000001.SZ", "trade_date": "2024-06-12", "adj_factor": 1.10}
  ]
}
```

- 字段：`code`（str）、`trade_date`（`YYYY-MM-DD` str）、`adj_factor`（float）
- 空结果：`{"data": []}`，HTTP 200
- 错误统一信封：`{"error": {"code", "message", "request_id"}}`（复用现有 `AppError` 处理）

### 错误码

| 场景 | HTTP | error.code |
|---|---|---|
| codes 为空 / 日期格式非法 / start > end | 422 | INVALID_REQUEST |
| SDK 未登录 | 503 | SDK_NOT_READY |
| SDK 查询失败 | 502 | SDK_QUERY_FAILED |
| SDK 并发上限 | 503 | SERVICE_BUSY |
| 其他未捕获 | 500 | INTERNAL_ERROR |

## 架构设计（三层）

完全类比现有 `/daily` 的三层结构。

### 1. 路由层 `http_app.py`

新增：
- `AdjFactorRequest` Pydantic 模型（`codes` / `start_time` / `end_time`，含 `codes_nonempty` 校验），类比 `DailyRequest`
- `POST /adj_factor` 路由处理器，结构与 `/daily` 完全一致：SdkGate 限流 → `asyncio.to_thread(adj_factor_service.query, ...)` → 统一错误处理
- `create_app` 中实例化 `AdjFactorService` 并挂到 `app.state`

### 2. 服务层 `app/adj_factor_service.py`（新文件）

类比 `KlineService`，职责：HTTP 日期参数 → SDK 调用 → 宽表 melt → 日期过滤 → 序列化。

```python
class AdjFactorService:
    def __init__(self, gateway: Gateway):
        self._gw = gateway

    def query(
        self,
        codes: list[str],
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict]:
        # 1. 解析日期（ISO → date），校验 start <= end
        # 2. 调 gateway.get_adj_factor(codes) 拿 SDK DataFrame
        # 3. melt + dropna → [{code, trade_date, adj_factor}]
        # 4. 按 start/end 过滤 trade_date
        # 5. 序列化返回
```

关键方法：

- `_parse_date(iso) -> date | None`：解析 ISO 日期/日期时间字符串为 `date` 对象；None 透传；格式非法抛 `ValueError`（→ HTTP 422）。
  - **与 `to_sdk_date` 的关系**：`to_sdk_date`（`kline_service.py:35-52`）返回 `int`（YYYYMMDD），本服务需要 `date` 对象用于比较，返回类型不同不能直接复用。实现时抽取共享的内部解析函数 `_parse_iso(iso) -> datetime`（基于 `datetime.fromisoformat`），`to_sdk_date` 和 `_parse_date` 都基于它构建，保证错误信息格式一致（`http_app.py:244-246` 的 `except ValueError` 依赖可读错误消息）。

- `_melt_and_normalize(df) -> pd.DataFrame`：把 SDK 返回的 DataFrame 规范成 `code/trade_date/adj_factor` 长表。判定顺序与边界处理伪码：

```python
def _melt_and_normalize(df) -> pd.DataFrame:
    if df is None or df.empty:
        return empty_df_with_cols(["code", "trade_date", "adj_factor"])
    # 判定顺序：先看是否已是长表（含 code + adj_factor 列），否则按宽表处理
    if "code" in df.columns and "adj_factor" in df.columns:
        # 长表形态：rename 兜底（timestamp/date → trade_date）
        df = rename_trade_date_column(df)
    else:
        # 宽表形态：index=交易日期, columns=股票代码
        df = df.reset_index()  # index → 列；index 无名时列名为 "index"
        date_col = df.columns[0]  # 按位置取日期列，不硬编码列名
        df = df.melt(id_vars=[date_col], var_name="code", value_name="adj_factor")
        df = df.rename(columns={date_col: "trade_date"})
    # dropna：兜底过滤非除权日（SDK 若返回稀疏宽表，NaN 行被清除）
    df = df.dropna(subset=["adj_factor"])
    # trade_date 统一为 YYYY-MM-DD 字符串（serialize_dataframe 会把 datetime 转 ISO datetime，
    # 需在此显式截断为日期，同 _truncate_kline_time_in_df 模式 kline_service.py:131-141）
    df["trade_date"] = to_date_str(df["trade_date"])  # datetime/str → "%Y-%m-%d"
    return df[["code", "trade_date", "adj_factor"]]
```

  边界情况：空 df（新股/无除权事件）、index 无名（`reset_index` 后列名 `"index"`）、index 为 datetime64（需 `strftime` 截断）、宽表无数据列（melt 后空 df）—— 均由上述顺序处理，不抛异常。

- `_filter_by_date(df, start, end) -> pd.DataFrame`：按 `trade_date`（已是 `YYYY-MM-DD` 字符串，可与 `date.isoformat()` 字符串比较）过滤，`start`/`end` 为 None 时该侧不过滤。

- 序列化复用 `serialize_dataframe`（`serializer.py`）。计时日志同 `KlineService.query`：区分 SDK 调用与后处理耗时。

计时日志同 `KlineService.query`：区分 SDK 调用与后处理耗时。

### 3. 网关层 `gateway.py`

`Gateway` Protocol 新增方法：
```python
def get_adj_factor(self, codes: list[str]) -> pd.DataFrame: ...
```

`AmazingDataGateway` 实现：
```python
def get_adj_factor(self, codes: list[str]) -> pd.DataFrame:
    if not self._ready or self._base_data is None:
        raise GatewayNotReadyError("gateway not ready")
    with self._lock:
        try:
            return self._base_data.get_adj_factor(
                codes,
                local_path=self._config.adj_factor_local_path,
                is_local=False,
            )
        except Exception as e:
            # 连接类错误惰性重连 + 重试一次（同 query_kline 模式）
            ...
```

- `is_local=False`：每次从服务端取最新（与 `/daily` 实时查语义一致，不依赖本地缓存）
- `local_path`：从 `Config.adj_factor_local_path` 读取（SDK 参数强制要求）
- 异常处理复用 `_is_connection_error` + 惰性重连模式
- 返回原始 DataFrame，不做 melt（melt 在 service 层）

## 配置变更 `config.py`

`Config` dataclass 新增字段（默认空字符串，与现有 Config "缺失值给空字符串/0 作为安全默认" 理念一致，`config.py:25`）：

```python
adj_factor_local_path: str = ""  # SDK get_adj_factor 的 local_path 参数，必须为绝对路径
```

`from_env` 读取：
```python
adj_factor_local_path=os.environ.get("ADJ_FACTOR_LOCAL_PATH", "") or "",
```

`is_configured` 不变（adj_factor_local_path 不影响启动判定）。

**SDK 强制要求 `local_path` 为绝对路径**（手册 3.5.2.6 注(1)：`类似 'D://AmazingData_local_data//'，只写文件夹的绝对路径即可`）。因此 `AmazingDataGateway.get_adj_factor` 调用前需校验：
- `adj_factor_local_path` 非空，否则抛 `GatewayNotReadyError("adj_factor_local_path not configured")` → HTTP 503
- 生产部署必须通过 `ADJ_FACTOR_LOCAL_PATH` 环境变量设置为可写的绝对路径

注意：SDK `is_local=False` 时仍会**写入** `local_path` 作为本地缓存（手册 3.5.2.6 注(2)：`False: 从互联网取数据，并更新本地 local_path 的数据`），因此该目录必须存在且可写，非纯实时查询。

## 测试策略

### FakeGateway 扩展 `tests/conftest.py`

- `FakeGateway` 新增 `get_adj_factor(codes)` 方法：记录调用、未就绪抛 `GatewayNotReadyError`、返回注入的测试 DataFrame
- 新增测试数据工厂 `make_adj_factor_df()`：返回宽表 DataFrame（`index=交易日期, columns=股票代码`），模拟 SDK 返回结构

### 测试用例 `tests/test_adj_factor.py`（新）

| 用例 | 验证点 |
|---|---|
| 宽表 melt | SDK 宽表 → 正确长表 `[{code, trade_date, adj_factor}]` |
| 宽表 index 无名 | `reset_index` 后按位置取日期列，不报错 |
| 宽表 index 为 datetime | trade_date 正确截断为 `YYYY-MM-DD`（非 ISO datetime） |
| dropna 过滤 | 宽表含 NaN 的非除权日被过滤 |
| 空 DataFrame（SDK 返回空） | 返回 200 + `{"data": []}`，不抛异常 |
| 日期过滤 | `start_time`/`end_time` 正确过滤 `trade_date` |
| start > end | 返回 422 |
| codes 为空 | 返回 422 |
| SDK 未就绪 | 返回 503 |
| adj_factor_local_path 未配置 | 返回 503（GatewayNotReadyError） |
| SDK 查询失败 | 返回 502 |
| 空结果（过滤后为空） | 返回 200 + `{"data": []}` |
| 字段名中性 | 响应字段为 `code`/`trade_date`/`adj_factor`，非 stocker 的 `symbol`/`ex_factor` |
| FakeGateway 满足 Protocol | `isinstance(FakeGateway(), Gateway)` 为 True（`@runtime_checkable`） |

## 关键决策汇总

| 决策点 | 选择 | 理由 |
|---|---|---|
| SDK 接口 | 3.5.2.6 `get_adj_factor`（单次） | stocker 需单次除权事件因子做前复权，schema 长表匹配 |
| `is_local` | `False` | 每次从服务端取最新数据（`True` 会返回陈旧本地数据，与 stocker 增量同步冲突）。注意 SDK `is_local=False` 仍会写入 `local_path` 缓存（手册注(2)），非纯实时查询，与 `/daily` 的无落盘查询不完全同构 |
| 日期过滤 | service 层按 `trade_date` 过滤 | SDK `get_adj_factor` 不支持日期参数，必须服务端过滤 |
| 非除权日处理 | `dropna` + 过滤 `adj_factor != 1.0` | 实测 SDK 返回**密集宽表**（每个交易日一行），非除权日 adj_factor=1.0；dropna 兜底稀疏表，`!= 1.0` 过滤密集表非除权日。用精确 `!= 1.0` 而非 `np.isclose`：实测有 0.9955 等 <1.0 真事件，isclose 会误删。详见 `adj_factor_service._filter_non_event_rows` |
| 进程内缓存 | 不做（YAGNI） | 先简单正确；性能问题实测后再加 |
| 字段命名 | `code`/`trade_date`/`adj_factor` | 中性，与 `/daily` 的 `code` 一致；stocker 通过 field_map 适配 |
| 参数命名 | `codes`/`start_time`/`end_time` | 与 `/daily` 一致；stocker 配 `symbols_param: codes` 适配 |

## stocker 适配 YAML（证明满足需求）

stocker 端配置即可对接，amazingDataHttp 不做任何 stocker 专用适配：

```yaml
adj_factor:
  url: http://<amazingDataHttp>:3021/adj_factor
  method: POST
  batch: 100
  rpm: 200
  response_path: data
  symbols_param: codes              # 适配 amazingDataHttp 的 codes（覆盖默认 symbols）
  start_param: start_time           # 与默认值相同，显式声明以消除歧义
  end_param: end_time               # 同上
  field_map:
    code: symbol                    # 适配 stocker 内部字段
    trade_date: trade_date
    adj_factor: ex_factor
  transforms:
    trade_date: "parse_date(value, '%Y-%m-%d')"
```

**`transforms.trade_date` 是必配项**：amazingDataHttp 返回的 `trade_date` 是 `YYYY-MM-DD` 字符串，stocker 的 `normalize_adj_factors`（`normalizer.py:72-82`）对非数值列走 `cast(pl.Date, strict=False)`，若漏配 `transforms` 则字符串无法解析为 Date，整列变 null 被 `drop_nulls()` 清空。

## 验证计划（实现后）

用真实 SDK 凭证调一次 `base_data.get_adj_factor(['000001.SZ'])`，确认：
1. 返回宽表（`index=日期, columns=代码`）还是长表
2. 非除权日是否有数据（验证"只返回除权事件"假设）
3. 索引/列命名、index 是否有名
4. 字段类型（index 是 datetime 还是字符串）
5. **性能**：单次 100 只股票的 SDK 查询耗时与 melt 后内存峰值，评估 `SdkGate(max_concurrent=5)` 是否需调整

若假设错误（非除权日有数据），在 `_melt_and_normalize` 的 `dropna` 调整即可，**接口契约不变**。

## 运维注意事项

- **`ADJ_FACTOR_LOCAL_PATH` 必须配置为可写的绝对路径**：SDK `is_local=False` 时仍会写入该目录作为缓存（手册 3.5.2.6 注(2)），目录不存在或只读会导致 SDK 抛异常。
- **磁盘增长**：SDK 会持续往 `local_path` 写缓存数据，无自动清理机制。运维需规划磁盘容量，或定期清理该目录（清理后下次请求 SDK 会重新从服务端拉取并重写）。
- **多实例部署**：若 amazingDataHttp 横向扩展多实例，每个实例必须用独立的 `ADJ_FACTOR_LOCAL_PATH`，避免多进程并发写同一目录导致文件竞争。
- **首次请求较慢**：`local_path` 为空时 SDK 从服务端全量拉取，首次请求耗时较长；后续请求 SDK 会用本地缓存加速（即使 `is_local=False` 也会读缓存判断是否需要更新）。

## 非目标

- 不做进程内缓存 / 本地 parquet 落盘（stocker 自己落盘到 `all.parquet`）
- 不做字段重命名映射（由 stocker field_map 完成）
- 不做复权价计算（stocker 自行累乘）
- 不暴露 `is_local` / `local_path` 给 HTTP 客户端（SDK 内部参数，服务端管理）
- 不支持 ETF 复权因子（stocker 的 `adj_factor_etf` 目录由 asset_type 区分；首期只做股票，ETF 后续按需扩展）

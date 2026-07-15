# AmazingData HTTP 适配服务设计规格

> 状态：待审阅
> 日期：2026-07-13
> 范围：首期日 K HTTP 接入，预留分钟 K、周 K、月 K 等周期扩展

## 1. 背景与目标

现有项目的自定义数据源只通过 HTTP 访问，当前支持 `daily`、`adj_factor` 和 `realtime` 三类数据集。项目通过 HTTP 响应中的 `field_map` 将上游字段映射为内部标准字段。

AmazingData SDK（wheel 包，按 Python 版本选用 `cp313`/`cp314`）提供 Python API，依赖 `tgw`，能够访问历史 K 线等数据。目标是新增一个独立的 Python HTTP 适配服务，让项目通过既有自定义数据源协议获取 AmazingData 的日 K 数据。

适配服务不负责项目字段重命名或业务加工，只负责：

- 接收项目约定的 HTTP 请求；
- 调用 AmazingData SDK；
- 将 SDK 返回的 DataFrame / Python 对象转换为 JSON 安全数据；
- 保留上游业务字段名，交由项目 YAML 的 `field_map` 完成映射；
- 提供健康检查和可诊断的错误响应。

## 2. 首期范围

### 2.1 首期实现

- `POST /daily`：按代码和日期区间查询日 K；
- `GET /health`：检查配置、SDK 初始化和登录状态；
- Linux x86_64 Docker 部署；
- 使用 `AmazingData-*-cp314-none-any.whl`，并安装其依赖；
- SDK 返回结果的通用 JSON 序列化；
- 单元测试和无真实账号的集成测试；
- 凭据通过环境变量或 Docker secrets 注入。

### 2.2 明确不在首期范围

- 复权因子接口；
- 实时行情订阅；
- 财务、盘口、基金、期权等其他数据集；
- 项目内部字段转换、单位转换和复权计算；
- 数据持久化、定时任务和缓存服务；
- 对现有主项目代码进行无关重构。

### 2.3 后续扩展约束

内部 K 线查询能力按周期参数设计，至少预留：

- `day`；
- `min1`、`min3`、`min5`、`min10`、`min15`、`min30`、`min60`、`min120`；
- `week`、`month`、`season`、`year`。

首期只对外暴露 `/daily`，不提前暴露主项目当前契约未定义的分钟或周 K 路由。未来新增数据源契约后，再增加对应路由或通用 `/kline` 路由。

## 3. 总体架构

```text
主项目数据同步任务
    │ POST /daily
    ▼
AmazingData HTTP 适配服务
    ├── HTTP 路由层：请求校验、响应和错误码
    ├── KlineService：日期、代码和周期参数转换
    ├── AmazingDataGateway：登录、MarketData 初始化、SDK 调用
    ├── Serializer：DataFrame / NumPy / datetime → JSON 安全值
    └── HealthService：配置、登录和初始化状态
             │
             ▼
       AmazingData SDK 1.1.7
             │
             ▼
       tgw 原生数据服务
```

服务以常驻进程运行。启动时加载环境变量并尝试登录 SDK；HTTP 请求不重复创建 SDK 对象。SDK 的会话与原生库对象由进程级 gateway 管理，避免每个请求重复登录和初始化。

### 3.1 适配层职责边界

适配服务负责：

- 将 HTTP 输入的 ISO 日期转换为 SDK 所需的日期参数；
- 将标准代码列表传给 SDK；
- 选择 SDK 的日 K 周期；
- 展平 SDK 返回的 `dict[code, DataFrame]` 或等价结构；
- 处理索引、时间、缺失值和 NumPy 标量的 JSON 序列化；
- 统一处理 SDK 调用异常、超时和未登录状态。

适配服务不负责：

- 将 `code` 改名为 `symbol`；
- 将 `trade_time` 改名为 `date`；
- 将 `volume` 改名为 `vol`；
- 股票代码市场后缀转换；
- 价格、成交量、成交额单位换算；
- 复权、涨跌幅、指标或其他业务计算。

这里的“原始返回”定义为：**保留 SDK 业务字段名和业务值语义，但完成 HTTP 传输必需的结构与类型序列化**。DataFrame 本身、索引对象、NumPy 标量和 NaN 无法直接作为 JSON，因此不承诺 Python 对象级原样透传。

## 4. HTTP 契约

### 4.1 日 K 查询

```http
POST /daily
Content-Type: application/json
```

请求体：

```json
{
  "symbols": ["000001.SZ", "600000.SH"],
  "start_time": "2024-01-01",
  "end_time": "2024-01-31"
}
```

字段约束：

- `symbols` 必须是非空字符串数组；
- 服务保留调用方提供的代码字符串，不在适配层做代码标准化；
- `start_time` 和 `end_time` 接受项目协议规定的日期格式，首期为 `YYYY-MM-DD`；
- `start_time` 不得晚于 `end_time`；
- 首期按项目自定义数据源协议由上游批量请求调用，服务本身不负责再切分批次。

成功响应：

```json
{
  "data": [
    {
      "code": "000001.SZ",
      "trade_time": "2024-01-02",
      "open": 10.2,
      "high": 10.45,
      "low": 10.1,
      "close": 10.3,
      "volume": 1234567,
      "amount": 12700000
    }
  ]
}
```

示例字段仅用于说明序列化形态，最终字段名、索引字段和时间格式必须以安装 `AmazingData 1.1.7` 后的真实探测结果为准。服务不得为了匹配示例而擅自重命名 SDK 字段。

空结果返回 HTTP `200` 和 `{"data": []}`，表示请求有效但没有数据，不应被当作服务故障。

### 4.2 健康检查

```http
GET /health
```

健康响应返回 HTTP `200`，至少包含：

```json
{
  "status": "ok",
  "sdk": "ready"
}
```

配置缺失、SDK 导入失败、登录失败或初始化未完成时返回 HTTP `503`，响应不得包含密码或完整连接凭据。

### 4.3 错误响应

统一错误结构：

```json
{
  "error": {
    "code": "SDK_QUERY_FAILED",
    "message": "查询日K失败",
    "request_id": "..."
  }
}
```

错误码初步定义：

- `INVALID_REQUEST`：请求体、代码列表或日期参数无效；HTTP `422`；
- `SDK_NOT_READY`：SDK 尚未成功初始化或登录；HTTP `503`；
- `SDK_QUERY_FAILED`：上游 SDK 查询失败；HTTP `502`；
- `SERIALIZATION_FAILED`：SDK 返回值无法安全序列化；HTTP `502`；
- `INTERNAL_ERROR`：未分类的服务内部错误；HTTP `500`。

错误日志保留路由、代码数量、日期区间、周期和异常类型等上下文，不记录密码；代码列表过长时记录数量和摘要，不完整记录请求体。

## 5. SDK 适配与前置探测

### 5.1 wheel 与运行时

当前目录包含：

- `AmazingData-*-cp314-none-any.whl`；
- `tgw-*-py3-none-any.whl`。

`AmazingData` wheel 的元数据声明：

- 包名：`AmazingData`；
- Python 标记：`cp314`；
- 依赖：`pydantic>=2.6.4`、`numba>=0.65.0`、`scipy>=1.15.1`、`tgw`。

wheel 内容包含 `AmazingData.query_api.market_data`、`AmazingData.query_api.base_data`、`AmazingData.subscribe_api` 等模块，与整理后的 SDK 文档中的 `AmazingData` API 结构一致。

Docker 镜像优先固定为 Linux x86_64、Python 3.14；原因是 `AmazingData` wheel 明确标记为 `cp314`。实施前必须确认 `tgw` 在该 Linux x86_64 + Python 3.14 组合下存在可安装且兼容的 wheel。若供应商仅提供 Python 3.13 的 Linux 原生依赖，则必须退回使用兼容的 SDK 版本或由用户补充对应的 Linux wheel，不能在设计阶段假定跨 Python 小版本兼容。

### 5.2 实际 API 探测门禁

在编写 HTTP 业务代码前，必须在目标 Docker 基础环境中完成最小探测：

1. 安装两个本地 wheel 及其依赖；
2. 执行 `import AmazingData as ad`；
3. 确认 `login` 的实际参数名称和端口参数名称；
4. 确认 `BaseData`、`get_calendar` 和 `MarketData` 的实际导出位置；
5. 使用受控账号完成登录；
6. 获取交易日历并创建 `MarketData`；
7. 对一个代码、一个交易日调用日 K 查询；
8. 记录返回容器类型、DataFrame 列名、索引名称、代码格式、时间类型、缺失值表现和异常类型；
9. 登出并确认进程可正常退出。

探测脚本只输出结构摘要，不输出账号、密码或完整行情数据。探测结果将作为 gateway 和 serializer 的实现依据；如果真实接口与整理文档不同，以 wheel 的实际行为为准。

### 5.3 Gateway 接口

HTTP 层不直接依赖 AmazingData 的具体模块路径，而依赖内部 gateway 接口：

```text
login() -> None
logout() -> None
is_ready() -> bool
query_kline(symbols, begin_date, end_date, period) -> SDK result
```

`AmazingDataGateway` 负责将 SDK 具体 API 封装在单一边界内。这样可以：

- 让 HTTP 层不感知 SDK 对象创建细节；
- 用 fake gateway 做自动化测试；
- 在 wheel 版本升级时集中修改适配代码；
- 将未来分钟、周、月 K 的周期映射集中管理。

### 5.4 周期映射

内部使用字符串到 SDK 枚举值的显式白名单映射，不允许 HTTP 客户端直接传入任意整数：

```text
 day    -> Period.day.value
 min1   -> Period.min1.value
 min3   -> Period.min3.value
 min5   -> Period.min5.value
 min10  -> Period.min10.value
 min15  -> Period.min15.value
 min30  -> Period.min30.value
 min60  -> Period.min60.value
 min120 -> Period.min120.value
 week   -> Period.week.value
 month  -> Period.month.value
 season -> Period.season.value
 year   -> Period.year.value
```

首期 `/daily` 固定选择 `day`，不接受请求体中的任意周期参数。映射表可以先在内部建立，但未被真实探测验证的周期不得在对外接口中宣称可用。

## 6. 登录生命周期与并发

### 6.1 启动与健康状态

- 服务启动时读取环境变量并尝试登录；
- 配置缺失或登录失败时，进程仍可启动，以便 Docker 日志和 `/health` 暴露诊断信息，但 `/health` 返回 `503`；
- 登录成功且交易日历 / `MarketData` 初始化成功后，`/health` 返回 `200`；
- 初始化过程不得在日志中打印密码。

### 6.2 会话失效

每次查询前检查 gateway 状态。若 SDK 明确报告会话失效：

1. 获取登录锁；
2. 再次确认其他请求是否已经完成重登录；
3. 执行一次登出（若 SDK 支持且安全）和登录；
4. 仅重试原查询一次；
5. 重登录失败则返回 `SDK_NOT_READY` 或 `SDK_QUERY_FAILED`，不无限重试。

登录重试采用退避策略，避免上游异常时形成登录风暴。所有并发查询共享同一个 gateway；若实测原生 SDK 非线程安全，则通过互斥锁或受限线程池串行化 SDK 调用。

### 6.3 超时与取消

SDK 若提供超时参数，优先使用 SDK 的超时能力。若不提供，则通过受限执行线程和服务侧超时控制请求等待时间；超时后不得强制终止可能仍在使用原生库的线程，避免破坏进程状态。需要将该请求标记为失败，并由后续健康检查决定是否需要重建 gateway 或重启容器。

## 7. Docker 部署设计

### 7.1 镜像

- 目标平台：`linux/amd64`；
- Python：优先 `3.14`，与 `AmazingData` wheel 的 `cp314` 标记匹配；
- 基础镜像：先使用完整的 Debian 系列 Python 镜像进行联调，确认依赖后再考虑 `slim`；
- 镜像内安装本地 `tgw` wheel、`AmazingData` wheel 和 Web 服务依赖；
- 通过 Docker build 的平台参数固定 AMD64，避免 ARM 主机产生不兼容镜像。

### 7.2 环境变量

首期使用以下环境变量：

```text
AMAZINGDATA_USERNAME
AMAZINGDATA_PASSWORD
AMAZINGDATA_IP
AMAZINGDATA_PORT
HTTP_HOST=0.0.0.0
HTTP_PORT=3021
```

真实凭据只通过运行时环境、`env_file` 或 Docker secrets 注入；不写入 Dockerfile、镜像层、示例 YAML、源码或测试快照。

### 7.3 网络

容器必须能够访问 AmazingData / tgw 要求的服务器 IP 与端口。主项目通过 Docker Compose 服务名访问适配服务，例如：

```text
http://amazingdata-http:3021/daily
```

适配服务端口不应直接暴露到公网；如需跨主机访问，应使用受控内网和网络 ACL。

## 8. 测试与验收

### 8.1 单元测试

使用 fake gateway，不依赖真实账户，覆盖：

- 日 K 请求体校验；
- 日期 `YYYY-MM-DD` 到 SDK `YYYYMMDD` 的转换；
- 空代码列表、非法日期和反向日期区间；
- `day` 周期映射；
- `dict[code, DataFrame]` 展平；
- DataFrame 索引或列中的时间序列化；
- Python / NumPy 整数、浮点数和布尔值序列化；
- `NaN` / `NaT` 转换为 `null`；
- 空 DataFrame 返回 `data: []`；
- SDK 异常映射为 `502`；
- 未登录状态映射为 `503`；
- 健康检查在 ready / not ready 两种状态下的返回码。

### 8.2 SDK 探测测试

在目标 Docker 环境中使用真实 SDK 做一次性联调，要求：

- 能导入 `AmazingData`；
- 能登录并创建 `MarketData`；
- 单标的日 K 查询成功；
- 记录结果结构摘要；
- 能登出或正常释放资源。

该测试需要真实账户和数据服务网络，不应作为普通 CI 的无凭据自动化步骤。

### 8.3 端到端验收

- Docker 容器在 Linux AMD64 上成功启动；
- `/health` 反映真实 SDK 状态；
- `POST /daily` 返回 `{"data": [...]}`；
- 主项目自定义数据源“试拉测试”能解析 `response_path: data`；
- 主项目 YAML 使用正确方向的 `field_map`，例如：

```yaml
field_map:
  code: symbol
  trade_time: date
  open: open
  high: high
  low: low
  close: close
  volume: volume
  amount: amount
```

- 适配服务返回 SDK 原字段，不输出主项目专用字段名；
- 日 K 数据能被主项目后续存储和处理链路接受；
- 错误时能从 `request_id`、服务日志和状态码定位问题，且不泄露凭据。


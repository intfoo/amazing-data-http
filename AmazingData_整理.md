# AmazingData 手册整理版（仅 3.5 / 4.1 / 4.2）

> 说明：本文件由 PDF 文本抽取后进行结构化整理，重点优化了标题层级、参数表格、代码示例的可读性。

## 3.5 API 接口详细
### 3.5.1 基础接口
#### 3.5.1.1 登录
调用任何数据接口之前，必须先调用登录接口。SDK 的账号、密码、ip 和端口号需联系您的开户营业部申请开通权限之后获取。  
**函数接口**：`login` 
**功能描述**：api 登陆  
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| username | str | 是 | 账号 |
| password | str | 是 | 密码 |
| ip | str | 是 | 服务器ip |
| host | int | 是 | 服务器端口号 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
```
#### 3.5.1.2 登出
**函数接口**：`logout`
**功能描述**：api 退出登录链接
#### 3.5.1.3 更新密码
**函数接口**：`update_password`  
**功能描述**：更新密码接口，必须先登录才能修改密码  
**参数/字段**
| 名称 | 类型 | 说明 |
|---|---|---|
| username | str | 用户名 |
| old_password | str | 旧密码 |
| new_password | str | 新密码 |

### 3.5.2 基础数据
#### 3.5.2.1 每日最新证券信息
**函数接口**：`get_code_info`
**功能描述**：获取每日最新证券信息，交易日早上9点前更新当日最新
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| security_type | str | 否 | 代码类型security_type（见附录），默认为EXTRA_STOCK_A（上交所A股、深交所A股和北交所的股票列表）|

**输出:**  
| 参数 | 数据类型 | 解释 |
|---|---|---|
| code_info | dataframe |index为股票代码，column为symbol(证券简称)，pre_close(昨收价)，high_limited(涨停价)，low_limited(跌停价)，price_tick(最小价格变动单位)|  

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_info = base_data_object.get_code_info(security_type='EXTRA_ETF')
```
#### 3.5.2.2 每日最新代码表（沪深北）
交易日早上9点前更新  
**函数接口**：`get_code_list`  
**功能描述**：获取代码表（每日最新），此接口无法获取历史代码表  
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| security_type | str | 否 | 代码类型security_type（见附录），默认为EXTRA_STOCK_A（上交所A股、深交所A股和北交所的股票列表）|  


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| code_list | list | 证券代码 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
```
#### 3.5.2.3 每日最新代码表（期货交易所）
交易日早上9 点前更新
**函数接口**：`get_future_code_list`
**功能描述**：获取代码表（每日最新），此接口无法获取历史代码表

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| security_type | str | 是 | 代码类型security_type(期货交易所)（见附录），默认为EXTRA_FUTURE（期货, 包含中金所/上期所/大商所/郑商所/上海国际能源交易中心所）|


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| code_list | list | 证券代码 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_future_code_list(security_type='EXTRA_FUTURE')
```
#### 3.5.2.4 每日最新代码表（期权）
交易日早上9 点前更新
**函数接口**：`get_option_code_list`
**功能描述**：获取代码表（每日最新），此接口无法获取历史代码表
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| security_type | str | 是 | 代码类型security_type 期权)（见附录），默认为EXTRA_ETF_OP（ETF期权, 包含上交所和深交所）|


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| code_list | list | 证券代码 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_option_code_list(security_type='EXTRA_ETF_OP')
```
#### 3.5.2.5 复权因子（后复权因子）
**函数接口**：`BaseData.get_backward_factor`
**功能描述**：获取复权因子数据并本地存储，复权因子为根据交易所行情数据计算得出的后复权因子；

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 代码列表，支持股票、ETF |
| local_path | str | 是 | 本地存储复权因子数据的文件夹地址 |
| is_local | bool | 是 | 是否使用本地存储的数据，默认为True |


**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| backward_factor | dataframe | index 为交易日期，column 为股票代码 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
backward_factor = base_data_object.get_backward_factor(code_list, local_path='D://AmazingData_local_data//',
                                                    is_local=False)
```
#### 3.5.2.6 复权因子（单次复权因子）
**函数接口**：`BaseData.get_adj_factor`
**功能描述**：获取复权因子数据并本地存储，复权因子为根据交易所行情数据计算得出的单次复权因子；
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 代码列表，支持股票、ETF |
| local_path | str | 是 | 本地存储复权因子数据的文件夹地址 |
| is_local | bool | 是 | 是否使用本地存储的数据，默认为True |


**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| adj_factor | dataframe |index 为交易日期，column 为股票代码|

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
adj_factor = base_data_object.get_adj_factor(code_list, local_path='D://AmazingData_local_data//', is_local=False)
```
#### 3.5.2.7 历史代码表
**函数接口**：`BaseData 的 get_hist_code_list`  
**功能描述**：获取历史代码表，先检查本地数据，再从服务端补充，最后返回数据输入参数：

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| security_type | str | 是 | 默认为"EXTRA_STOCK_A_SH_SZ" 沪深A股，支持附录security_type(沪深北)和security_type(期货交易所) |


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| code_list | List[str] | 证券代码 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_hist_code_list(security_type='EXTRA_STOCK_A_SH_SZ', start_date=20240101, end_date=20240701, local_path=local_path)
```
#### 3.5.2.8 交易日历
**函数接口**：`get_calendar`  
**功能描述**：获取交易所的交易日历  

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| data_type | str | 否 | 选择返回数据的类型，默认为str ，可选datetime 或str|
| market | str | 否 |选择市场market（见附录），默认为SH（上海）|


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| calendar | List[int] | 日期 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
calendar = base_data_object.get_calendar()
```
#### 3.5.2.9 证券基础信息
**函数接口**：`get_stock_basic`  
**功能描述**：获取指定股票列表的上市公司的证券基础数据，包含沪深北三个交易所，所有股票（包含已退市标的）的中英文名称、上市日期、退市日期、上市板块等信息  
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深北三个交易所的代码列表，可见示例|


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| stock_basic |dataframe| column 为stock_basic 的字段,index 为序号（无意义）| 

stock_basic 的字段说明： 
| 参数 | 数据类型 | 项目 | 解释 |
|---|---|---|---|
| MARKET_CODE | string | 证券代码 |
| SECURITY_NAME | string | 证券简称 |
| COMP_NAME | string | 证券中文名称 |
| PINYIN | string | 中文拼音简称 |
| COMP_NAME_ENG | string | 证券英文名称 |
| LISTDATE | int | 上市日期 |
| DELISTDATE | int | 退市日期 |
| LISTPLATE_NAME | string | 上市板块名称 |
| COMP_SNAME_ENG | string | 英文名称缩写 |
| IS_LISTED | int | 上市状态 | 1：上市交易  3：终止上市 |  |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A_SH_SZ')
stock_basic = info_data_object.get_stock_basic(code_list)
```
#### 3.5.2.10 历史证券信息
**函数接口**：`get_history_stock_status`  
**功能描述**：获取指定股票列表的上市公司的历史证券数据，以日度为频率，包含历史的涨跌停、st、除权除息等信息  
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
|is_local | bool | 否 |默认为True，本地数据缓存方案|
| begin_date | int | 否 | 交易日 |
| end_date | int | 否 | 交易日 |


**输出**
| 返回值 | 数据类型 | 解释 |
|---|---|---|
| history_stock_status | dataframe | column 为history_stock_status 的字段，index为序号（无意义）|

 history_stock_status 的字段说明：
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| MARKET_CODE | string | 证券代码 |
| TRADE_DATE | string | 日期 |
| PRECLOSE | float | 前收价 |
| HIGH_LIMITED | float | 涨停价 |
| LOW_LIMITED | float | 跌停价 |
| PRICE_HIGH_LMT_RATE | float | 涨停价上限 |
| PRICE_LOW_LMT_RATE | float | 跌停价下限 |
| IS_ST_SEC | string | 是否ST | 1 表示是，0 表示否 
| IS_SUSP_SEC | string | 是否停牌 | 1 表示是，0 表示否 
| IS_WD_SEC | string | 是否除息 | 1 表示是，0 表示否 |
| IS_XR_SEC | string | 是否除权 | 1 表示是，0 表示否 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
base_data_object = ad.BaseData()
calendar = base_data_object.get_calendar()
today = calendar[-1]
all_code_list = base_data_object.get_hist_code_list(security_type='EXTRA_STOCK_A_SH_SZ', start_date=20130101,
                                             end_date=today)
history_stock_status = info_data_object.get_history_stock_status(all_code_list)
```
#### 3.5.2.11 北交所新旧代码对照表
**函数接口**：`get_bj_code_mapping`
**功能描述**：获取北交所的存量上市公司股票新旧代码对照表
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，首选从本地读取，读取失败再从服务器取数据 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| bj_code_map | dataframe | column为bj_code_mapping的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
bj_code_mapping = info_data_object.get_bj_code_mapping()
```
bj_code_mapping 的字段说明：

**参数/字段**
| 字段名称 | 类型 | 字段说明 |
|---|---|---|
| OLD_CODE | string | 旧代码 |
| NEW_CODE | string | 新代码 |
| SECURITY_NAME | string | 证券简称 |
| LISTING_DATE | int | 上市日期 |

### 3.5.3 实时行情数据
实时行情订阅接口使用步骤
（1） 实例化AmazingData 的SubscribeData
（2） 回调函数的装饰器传入code_list(代码表)和period(数据周期)两个参数
（3） 回调函数中获取数据
#### 3.5.3.1 指数实时快照
**函数接口**：`onSnapshotindex`
**功能描述**：交易所指数快照数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持北交所、上交所、深交所的指数 |
| period | Period | 是 | Period.snapshot.value |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | 指数为SnapshotIndex（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_INDEX_A')
# 实时订阅
sub_data = ad.SubscribeData()
@sub_data.register(code_list=code_list, period=ad.constant.Period.snapshot.value)
def onSnapshotindex(data: Union[ad.constant.Snapshot, ad.constant.SnapshotIndex], period):
    print(period, data)
sub_data.run()
```
#### 3.5.3.2 股票实时快照
**函数接口**：`onSnapshot`
**功能描述**：level-1 快照数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持北交所、上交所、深交所的股票 |
| period | Period | 是 | Period.snapshot.value |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | 股票为Snapshot（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
# 实时订阅
sub_data = ad.SubscribeData()
@sub_data.register(code_list=code_list, period=ad.constant.Period.snapshot.value)
def onSnapshot(data: Union[ad.constant.Snapshot, ad.constant.SnapshotIndex], period):
    print(period, data)
sub_data.run()
```
#### 3.5.3.3 期货实时快照
**函数接口**：`onSnapshotfuture`
**功能描述**：level-1 快照数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持中金所/上期所/大商所/郑商所/上海国际能源交易中心所 |
| period | Period | 是 | Period.snapshotfuture.value |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | 期货为SnapshotFuture（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_FUTURE')
# 实时订阅
sub_data = ad.SubscribeData()
@sub_data.register(code_list=code_list, period=ad.constant.Period.snapshotfuture.value)
def onSnapshotfuture(data: Union[ad.constant.SnapshotFuture], period):
    print(period, data)
sub_data.run()
```
#### 3.5.3.4 ETF 实时快照
**函数接口**：`onSnapshotetf`
**功能描述**：level-1 快照数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持上交所、深交所的ETF |
| period | Period | 是 | Period.snapshot.value |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | ETF 为Snapshot（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_ETF')
# 实时订阅
sub_data = ad.SubscribeData()
@sub_data.register(code_list=code_list, period=ad.constant.Period.snapshot.value)
def onSnapshotetf(data: Union[ad.constant.Snapshot, ad.constant.SnapshotIndex], period):
    print(period, data)
sub_data.run()
```
#### 3.5.3.5 可转债实时快照
**函数接口**：`onSnapshotkzz`
**功能描述**：level-1 快照数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持上交所、深交所的可转债 |
| period | Period | 是 | Period.snapshot.value |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | 可转债为Snapshot（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_KZZ')
# 实时订阅
sub_data = ad.SubscribeData()
@sub_data.register(code_list=code_list, period=ad.constant.Period.snapshot.value)
def onSnapshotkzz(data: Union[ad.constant.Snapshot, ad.constant.SnapshotIndex], period):
    print(period, data)
sub_data.run()
```
#### 3.5.3.6 港股通实时快照
**函数接口**：`onSnapshothkt`
**功能描述**：港股通快照数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register

**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持上交所、深交所的港股通 |
| period | Period | 是 | Period.snapshotHKT.value |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | 港股通为SnapshotHKT（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_HKT')
# 实时订阅
sub_data = ad.SubscribeData()
@sub_data.register(code_list=code_list, period=ad.constant.Period.snapshotHKT.value)
def onSnapshothkt(data: Union[ad.constant.SnapshotHKT], period):
    print(period, data)
sub_data.run()
```
#### 3.5.3.7 实时K 线
**函数接口**：`OnKLine`
**功能描述**：K 线数据的实时订阅回调函数
输入参数：入参需传入装饰器中SubscribeData.register
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持北交所、上交所、深交所的可转债、股票、指数、ETF等品种，支持期货（中金所/上期所/大商所/郑商所/上海国际能源交易中心所） |
| period | Period | 是 | Period（见附录） |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| data | Object | Kline（见附录） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
# 实时订阅
sub_data = ad.SubscribeData()
# K 线
@sub_data.register(code_list=code_list, period=ad.constant.Period.min1.value)
def OnKLine(data: Union[ad.constant.Kline], period):
    print('OnKLine: ', data)
sub_data.run()
```
### 3.5.4 历史行情数据
（1） 实例化AmazingData 的MarketData，入参需交易日历
（2） 调用MarketData 的方法获取数据

#### 3.5.4.1 历史快照
**函数接口**：`query_snapshot`
**功能描述**：快照数据的历史数据查询接口
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持北交所、上交所、深交所的可转债、股票、指数、ETF、港股通等、ETF期权等品种 |
| begin_date | int | 是 | 日期，填写8位的整型格式的日期，比如20240101 |
| end_date | int | 是 | 日期，填写8位的整型格式的日期，比如20240201 |
| begin_time | int | 否 | 时分秒毫秒的时间戳，填写8位或9位的整型格式的日期，时占一位或两位，分占两位，秒占两位，毫秒占三位，例如9点整为90000000, 17点25分为172500000 |
| end_time | int | 否 | 时分秒毫秒的时间戳，例如17点30分为173000000 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| snapshot_dict | dict | 字典格式，key为代码，value为dataframe，column为快照数据，index为日期 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
calendar = base_data_object.get_calendar()
market_data_object = ad.MarketData(calendar)
snapshot_dict = market_data_object.query_snapshot(code_list, begin_date=20240530, end_date=20240530)
```
#### 3.5.4.2 历史K 线
**函数接口**：`query_kline`
**功能描述**：K 线数据的实时订阅回调函数 ，支持全部周期的K 线数据查询
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 可传入列表，支持北交所、上交所、深交所的可转债、股票、指数、ETF等品种，支持期货（中金所/上期所/大商所/郑商所/上海国际能源交易中心所） |
| begin_date | int | 是 | 日期，填写8位的整型格式的日期，比如20240101 |
| end_date | int | 是 | 日期，填写8位的整型格式的日期，比如20240201 |
| period | Period | 是 | 数据周期Period（见附录） |
| begin_time | int | 否 | 时分的时间戳，填写3位或4位的整型格式的日期，时占一位或两位，分占两位，例如9点整为900, 17点25分为1725 |
| end_time | int | 否 | 时分的时间戳，例如17点30分为1730 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| kline_dict | dict | 字典格式，key为代码，value为dataframe，column为K线数据，index为日期 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
base_data_object = ad.BaseData()
code_list = base_data_object.get_code_list(security_type='EXTRA_STOCK_A')
calendar = base_data_object.get_calendar()
market_data_object = ad.MarketData(calendar)

kline_dict = market_data_object.query_kline(code_list, begin_date=20240530, end_date=20240530)
```
### 3.5.5 财务数据
#### 3.5.5.1 资产负债表
**函数接口**：`get_balance_sheet`
**功能描述**：获取指定股票列表的上市公司的资产负债表数据
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 报告期 |
| end_date | int | 否 | 报告期 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| balance_sheet | dict | key：code，value：dataframe，column为balance_sheet的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
base_data_object = ad.BaseData()
calendar = base_data_object.get_calendar()
today = calendar[-1]
all_code_list = base_data_object.get_hist_code_list(security_type='EXTRA_STOCK_A_SH_SZ', start_date=20130101,
                                             end_date=today)
balance_sheet = info_data_object.get_balance_sheet(all_code_list)
```
#### 3.5.5.2 利润表
**函数接口**：`get_profit_statement`
**功能描述**：获取指定股票列表的上市公司的利润表数据
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 报告期 |
| end_date | int | 否 | 报告期 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| profit_statement | dict | key：code，value：dataframe，column为profit_statement的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
profit_statement = info_data_object.get_profit_statement(code_list, local_path=local_path, is_local=False)
```
#### 3.5.5.3 现金流量表
**函数接口**：`get_cash_flow_statement`
**功能描述**：获取指定股票列表的上市公司的现金流量表数据
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 报告期 |
| end_date | int | 否 | 报告期 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| cash_flow_statement | dict | key：code，value：dataframe，column为cash_flow_statement的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
cash_flow_statement = info_data_object.get_cash_flow_statement(code_list, local_path=local_path, is_local=False)
```
#### 3.5.5.4 股东权益变动表
**函数接口**：`get_stockholder_equity_change`
**功能描述**：获取指定股票列表的上市公司的股东权益变动表数据
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 报告期 |
| end_date | int | 否 | 报告期 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| stockholder_equity_change | dict | key：code，value：dataframe，column为stockholder_equity_change的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
stockholder_equity_change = info_data_object.get_stockholder_equity_change(code_list, local_path=local_path, is_local=False)
```
### 3.5.6 指数数据
#### 3.5.6.1 指数基本信息
**函数接口**：`get_index_basic`
**功能描述**：获取指数的基本信息
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持指数的代码列表 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| index_basic | dataframe | column为index_basic的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
index_basic = info_data_object.get_index_basic(code_list, local_path=local_path, is_local=False)
```
### 3.5.7 大宗交易数据
#### 3.5.7.1 大宗交易
**函数接口**：`get_block_trading`
**功能描述**：获取指定股票列表的上市公司的大宗交易数据
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 交易日 |
| end_date | int | 否 | 交易日 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| block_trading | dataframe | column为block_trading的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
block_trading = info_data_object.get_block_trading(code_list, local_path=local_path, is_local=False)
```
### 3.5.8 分红配股数据
#### 3.5.8.1 分红信息
**函数接口**：`get_dividend_info`
**功能描述**：获取指定股票列表的上市公司的分红信息
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 交易日 |
| end_date | int | 否 | 交易日 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| dividend_info | dataframe | column为dividend_info的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
dividend_info = info_data_object.get_dividend_info(code_list, local_path=local_path, is_local=False)
```
#### 3.5.8.2 配股信息
**函数接口**：`get_rights_info`
**功能描述**：获取指定股票列表的上市公司的配股信息
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深A 的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |
| begin_date | int | 否 | 交易日 |
| end_date | int | 否 | 交易日 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| rights_info | dataframe | column为rights_info的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
rights_info = info_data_object.get_rights_info(code_list, local_path=local_path, is_local=False)
```
### 3.5.9 基金数据
#### 3.9.1 基金基本信息
**函数接口**：`get_fund_basic`
**功能描述**：获取基金的基本信息
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持基金的代码列表 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| fund_basic | dataframe | column为fund_basic的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
fund_basic = info_data_object.get_fund_basic(code_list, local_path=local_path, is_local=False)
```
### 3.5.10 期权信息数据
#### 3.5.10.1 期权基本资料
**函数接口**：`get_option_basic_info`
**功能描述**：获取指定期权的基本资料（沪深交易所的ETF期权）
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深ETF期权的代码列表 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| basic_option_info | dataframe | column为option_basic_info的字段，index为序号（无意义） |

**字段说明：**
| 参数 | 数据类型 | 字段说明 | 备注 |
|---|---|---|---|
| CONTRACT_FULL_NAME | string | 合约全称 | |
| CONTRACT_TYPE | string | 合约类别 | C表示认购，P表示认沽 |
| DELIVERY_MONTH | string | 交割月份 | |
| EXPIRY_DATE | string | 到期日 | |
| EXERCISE_PRICE | float | 行权价格 | |
| EXERCISEENDDATE | string | 最后行权日 | |
| START_TRADE_DATE | string | 开始交易日 | |
| LISTING_REF_PRICE | float | 挂牌基准价 | |
| LAST_TRADE_DATE | string | 最后交易日 | |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoDataO()
base_data_object = ad.BaseDataO()
calendar = base_data_object.get_calendar()
today = calendar[-1]
code_list = base_data_object.get_option_code_list(security_type='EXTRA_ETF_OP')
hist_code_list = base_data_object.get_hist_code_list(security_type='EXTRA_ETF_OP', start_date=20130101, end_date=today)
option_basic_info = info_data_object.get_option_basic_info(code_list, is_local=False)
```
#### 3.5.10.2 期权标准合约属性
**函数接口**：`get_option_std_ctr_specs`
**功能描述**：获取指定期权标准合约属性（沪深交易所的ETF期权）
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深ETF的代码列表，目前包含159919.SZ、159915.SZ、159922.SZ、159901.SZ、510300.SH、588000.SH、588080.SH、510050.SH、510500.SH |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| option_std_ctr_specs | dataframe | column为option_std_ctr_specs的字段，index为序号（无意义） |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoDataO()
option_std_ctr_specs = info_data_object.get_option_std_ctr_specs(['510050.SH'], is_local=False)
```
#### 3.5.10.3 期权月合约属性变动表
**函数接口**：`get_option_mon_ctr_specs`
**功能描述**：获取指定期权月合约属性变动表（沪深交易所的ETF期权）
**输入参数**
| 参数 | 数据类型 | 必选 | 解释 |
|---|---|---|---|
| code_list | list[str] | 是 | 支持沪深ETF期权的代码列表，可见示例 |
| local_path | str | 是 | 本地存储数据的路径，需绝对路径，格式类似"D://AmazingData_local_data//" |
| is_local | bool | 否 | 默认为True，本地数据缓存方案 |

**输出**
| 参数 | 数据类型 | 解释 |
|---|---|---|
| block_trading | dataframe | column为block_trading的字段，index为序号（无意义） |

**block_trading的字段说明：**
| 参数 | 数据类型 | 字段说明 |
|---|---|---|
| MARKET_CODE | string | 证券代码 |
| TRADE_DATE | string | 交易日期 |
| B_SHARE_PRICE | float | 成交价（元） |
| B_SHARE_VOLUME | float | 成交量（万股） |
| B_FREQUENCY | int | 笔数 |
| BLOCK_AVG_VOLUME | float | 每笔成交数量（万股份） |
| B_SHARE_AMOUNT | float | 成交金额（万元） |
| B_BUYER_NAME | string | 买方营业部名称 |
| B_SELLER_NAME | string | 卖方营业部名称 |

```python
import AmazingData as ad
from typing import Union
ad.login(username='username', password='password', ip='***.***.***.***', port=****)
info_data_object = ad.InfoData()
block_trading = info_data_object.get_option_mon_ctr_specs(code_list, local_path=local_path, is_local=False)
```

## 4.1 字段取值说明
### 4.1.1 代码类型security_type(沪深北)
| 数据类型 | 枚举值 | 说明 |
|---|---|---|
| str | EXTRA_STOCK_A | 上交所A股、深交所A股和北交所的股票列表 |
| str | SH_A | 上交所A股的股票列表 |
| str | SZ_A | 深交所A股的股票列表 |
| str | BJ_A | 北交所的股票列表 |
| str | EXTRA_STOCK_A_SH_SZ | 上交所A股和深交所A股的股票列表 |
| str | EXTRA_INDEX_A_SH_SZ | 上交所和深交所指数列表 |
| str | EXTRA_INDEX_A | 上交所、深交所和北交所的指数列表 |
| str | SH_INDEX | 上交所指数列表 |
| str | SZ_INDEX | 深交所指数列表 |
| str | BJ_INDEX | 北交所的指数列表 |
| str | SH_ETF | 上交所的ETF 列表 |
| str | SZ_ETF | 深交所的ETF 列表 |
| str | EXTRA_ETF | 上交所、深交所的ETF列表 |
| str | SH_KZZ | 上交所的可转债列表 |
| str | SZ_KZZ | 深交所的可转债列表 |
| str | EXTRA_KZZ | 上交所、深交所的可转债列表 |
| str | SH_HKT | 沪港通 |
| str | SZ_HKT | 深港通 |
| str | EXTRA_HKT | 沪深港通 |

### 4.1.2 代码类型security_type(期货交易所)
| 数据类型 | 枚举值 | 说明 |
|---|---|---|
| str | EXTRA_FUTURE | 期货, 包含中金所/上期所/大商所/郑商所/上海国际能源交易中心所 |
| str | ZJ_FUTURE | 期货, 包含中金所 |
| str | SQ_FUTURE | 期货, 包含上期所 |
| str | DS_FUTURE | 期货, 包含大商所 |
| str | ZS_FUTURE | 期货, 包含郑商所 |
| str | SN_FUTURE | 期货, 包含海国际能源交易中心所 |

### 4.1.3 代码类型security_type(期权)
| 数据类型 | 枚举值 | 说明 |
|---|---|---|
| str | EXTRA_ETF_OP | ETF 期权, 上交所/深交所 |
| str | SH_OPTION | ETF 期货, 包含上交所 |
| str | SZ_OPTION | ETF 期货, 包含深交所 |

### 4.1.4 市场类型market
| 数据类型 | 枚举值 | 说明 |
|---|---|---|
| str | SH | 上交所 |
| str | SZ | 深交所 |
| str | BJ | 北交所 |
| str | SHF | 上期所 |
| str | CFE | 中金所 |
| str | DCE | 大商所 |
| str | CZC | 郑商所 |
| str | INE | 上海国际能源交易中心所 |
| str | SHN | 沪港通 |
| str | SZN | 深港通 |

### 4.1.5 交易阶段代码trading_phase_code
（1） 上海现货快照交易状态
该字段为8 位字符数组,左起每位表示特定的含义,无定义则填空格。
第0 位: 'S'表示启动(开市前)时段,'C'表示开盘集合竞价时段,'T'表示连续交易时段,'E'表示闭市时段,'P'表示产品停牌。
第1 位: '0'表示此产品不可正常交易,'1'表示此产品可正常交易。
第2 位: '0'表示未上市,'1'表示已上市。
第3 位: '0'表示此产品在当前时段不接受进行新订单申报,'1' 表示此产品在当前时段可接受进行新订单申报。

（2） 深圳现货快照交易状态
第 0 位: 'S'= 启动(开市前) 'O'= 开盘集合竞价 'T'= 连续竞价 'B'= 休市 'C'= 收盘集合竞价 'E'= 已闭市 'H'= 临时停牌 'A'= 盘后交易 'V'= 波动性中断。
第 1 位: '0'= 正常状态 '1'= 全天停牌。交易阶段代码

（3） 港股股票行情交易状态
'1'表示正常交易，'2'表示停牌，'3'表示复牌
（4） 上海期权快照交易状态
第 1 位： 'S'表示启动（开市前）时段， 'C'表示集合竞价时段，'T'表示连续交易时段，'B'表示休市时段， 'E'表示闭市时段， 'V'表示波动性中断， 'P'表示临时停牌、 'U'表示收盘集合竞价。 'M'表示可恢复交易的熔断（盘中集合竞价） ,'N'表示不可恢复交易的熔断（暂停交易至闭市）；
第 2 位： '0'表示未连续停牌，'1'表示连续停牌。（预留，暂填空格）；
第 3 位： '0'表示不限制开仓，'1'表示限制备兑开仓， '2'表示卖出开仓， '3'表示限制卖出开仓、备兑开仓， '4'表示限制买入开仓， '5'表示限制买入开仓、备兑开仓， '6'表示限制买入开仓、卖出开仓， '7'表示限制买入开仓、卖出开仓、备兑开仓；
第 4 位： '0'表示此产品在当前时段不接受进行新订单申报，'1' 表示此产品在当前时段可接受进行新订单申报。

### 4.1.6 数据周期Period
| 数据类型 | 枚举值 | 说明 |
|---|---|---|
| int | Period.min1.value | 1 分钟线 |
| int | Period.min3.value | 3 分钟线 |
| int | Period.min5.value | 5 分钟线 |
| int | Period.min10.value | 10 分钟线 |
| int | Period.min15.value | 15 分钟线 |
| int | Period.min30.value | 30 分钟线 |
| int | Period.min60.value | 60 分钟线 |
| int | Period.min120.value | 120 分钟线 |
| int | Period.day.value | 日线 |
| int | Period.week.value | 周线 |
| int | Period.month.value | 月线 |
| int | Period.season.value | 季度线 |
| int | Period.year.value | 年线 |

### 4.1.7 报告期名称REPORT_TYPE
| 报告期类型代码 | 报告期月份 |
|---|---|
| 3 | 3 月 |
| 6 | 6 月 |
| 9 | 9 月 |
| 12 | 12 月 |

### 4.1.8 报表类型代码表STATEMENT_TYPE
| 报表类型代码 | 报表类型 | 备注 |
|---|---|---|
| 1 | 合并报表 | 涵盖母公司的财务报表数据，为最新报表 |
| 2 | 合并报表(单季度) | 合并报表(单季度)=合并报表(本期)-合并报表(上一季) |
| 3 | 合并报表(单季度调整) | 合并报表(单季度调整)=合并报表(本期调整)-合并报表(上一季调整) |
| 4 | 合并报表(调整) | 本年度公布上年同期的财务报表数据，报告期为上年度 |
| 5 | 合并报表(更正前) | 即出更正公告后，把合并报表的记录修改为合并报表(更正前)；复制原来的记录，更正后报表类型改为合并报表 |
| 6 | 母公司报表 | 该公司母公司的财务报表数据 |
| 7 | 母公司报表(单季度) | 母公司报表(单季度)=母公司报表(本期)-母公司报表(上一季) |
| 8 | 母公司报表(单季度调整) | 母公司报表(单季度调整)=母公司报表(本期调整)-母公司报表(上一季调整) |
| 9 | 母公司报表(调整) | 该公司母公司的本年度公布上年同期的财务报表数据 |
| 10 | 母公司报表(更正前) | 之前上市公司已披露财务报表数据，但是由于某些特定原因导致出错，未调整之前的原始财务报表数据。 |

## 4.2 数据结构说明
### 4.2.1 Level-1 快照Snapshot
| 数据类型 | 字段名称 | 说明 |
|---|---|---|
| str | code | 证券代码+市场 |
| datetime | trade_time | 交易所行情数据时间 |
| float | pre_close | 昨收价 |
| float | last | 最新价 |
| float | open | 开盘价 |
| float | high | 最高价 |
| float | low | 最低价 |
| float | close | 收盘价 |
| float | volume | 成交总量 |
| float | amount | 成交总金额 |
| float | num_trades | 成交笔数 |
| float | high_limited | 涨停价 |
| float | low_limited | 跌停价 |
| float | ask_price1 | 卖1 档价格 |
| float | ask_price2 | 卖2 档价格 |
| float | ask_price3 | 卖3 档价格 |
| float | ask_price4 | 卖4 档价格 |
| float | ask_price5 | 卖5 档价格 |
| int | ask_volume1 | 卖1 档量 |
| int | ask_volume2 | 卖2 档量 |
| int | ask_volume3 | 卖3 档量 |
| int | ask_volume4 | 卖4 档量 |
| int | ask_volume5 | 卖5 档量 |
| float | bid_price1 | 买1 档价格 |
| float | bid_price2 | 买2 档价格 |
| float | bid_price3 | 买3 档价格 |
| float | bid_price4 | 买4 档价格 |
| float | bid_price5 | 买5 档价格 |
| int | bid_volume1 | 买1 档量 |
| int | bid_volume2 | 买2 档量 |
| int | bid_volume3 | 买3 档量 |
| int | bid_volume4 | 买4 档量 |
| int | bid_volume5 | 买5 档量 |
| float | iopv | 净值估产（仅基金品种有效） |
| str | trading_phase_code | 交易阶段代码 |

### 4.2.2 指数快照SnapshotIndex
| 数据类型 | 字段名称 | 说明 |
|---|---|---|
| str | code | 证券代码+市场 |
| datetime | trade_time | 交易所行情数据时间 |
| float | last | 最新价 |
| float | pre_close | 前收盘价 |
| float | open | 今开盘价 |
| float | high | 最高价 |
| float | low | 最低价 |
| float | close | 收盘价（仅上海有效） |
| int | volume | 成交总量（上交所:手，深交所:张） |
| float | amount | 成交总金额 |

### 4.2.3 港股通快照 SnapshotHKT
| 数据类型 | 字段名称 | 说明 |
|---|---|---|
| str | code | 证券代码+市场 |
| datetime | trade_time | 交易所行情数据时间 |
| float | pre_close | 昨收价 |
| float | last | 最新价 |
| float | high | 最高价 |
| float | low | 最低价 |
| float | volume | 成交总量 |
| float | amount | 成交总金额 |
| float | nominal_price | 暗盘价 |
| float | ref_price | 参考价 |
| float | bid_price_limit_up | 买盘上限价 |
| float | bid_price_limit_down | 买盘下限价 |
| float | offer_price_limit_up | 卖盘上限价 |
| float | offer_price_limit_down | 卖盘下限价 |
| float | high_limited | 冷静期价格上限 |
| float | low_limited | 冷静期价格下限 |
| float | ask_price1 | 卖1 档价格 |
| float | ask_price2 | 卖2 档价格 |
| float | ask_price3 | 卖3 档价格 |
| float | ask_price4 | 卖4 档价格 |
| float | ask_price5 | 卖5 档价格 |
| int | ask_volume1 | 卖1 档量 |
| int | ask_volume2 | 卖2 档量 |
| int | ask_volume3 | 卖3 档量 |
| int | ask_volume4 | 卖4 档量 |
| int | ask_volume5 | 卖5 档量 |
| float | bid_price1 | 买1 档价格 |
| float | bid_price2 | 买2 档价格 |
| float | bid_price3 | 买3 档价格 |
| float | bid_price4 | 买4 档价格 |
| float | bid_price5 | 买5 档价格 |
| int | bid_volume1 | 买1 档量 |
| int | bid_volume2 | 买2 档量 |
| int | bid_volume3 | 买3 档量 |
| int | bid_volume4 | 买4 档量 |
| int | bid_volume5 | 买5 档量 |
| str | trading_phase_code | 交易阶段代码 |

### 4.2.4 K 线Kline
| 数据类型 | 字段名称 | 说明 |
|---|---|---|
| str | code | 证券代码+市场 |
| datetime | kline_time | 交易所行情数据时间 |
| float | open | 今开盘价 |
| float | high | 最高价 |
| float | low | 最低价 |
| float | close | 收盘价 |
| int | volume | 成交总量 |
| float | amount | 成交总金额 |
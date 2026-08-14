# CTP ZeroMQ Proxy

独立的多客户端 CTP 接入服务。它持有唯一的 CTP MD/TD 会话，通过 ZeroMQ 向多个交易引擎和策略提供行情、订单、成交、资金及持仓数据。

本项目与 `ibkr-proxy` 同级，但当前不会改变或接管 `multi-market-trading-engine` 中原有的 CTP Gateway。

## 当前能力

- CTP 认证、登录、结算确认和断线状态通知。
- ZeroMQ PUB 行情及异步交易事件。
- ZeroMQ REP 查询、订阅、下单和撤单命令。
- 使用 `client_id + strategy_id + client_order_id` 标记订单归属。
- 使用 PostgreSQL 持久化客户端订单 ID 与 CTP 订单 ID 的映射。
- 多策略行情订阅引用计数。
- 资金、持仓和委托查询缓存及串行流控。
- CTP 不可用的平台仍可运行不依赖真实柜台的单元测试。

第一版只维护真实账户持仓，不提供策略级虚拟持仓分账。CTP 返回的资金和持仓始终是整个账户的数据。

## 架构

```text
CTP MD/TD Front
       |
       v
   ctp-proxy
   |    |       |
   |  PUB:5565  REP:5566
   |    |       |
   |    +--- trading engines / strategies
   |
PostgreSQL
```

PUB socket 只由单独的发布线程操作；CTP 原生回调先写入线程安全队列，避免跨线程使用 ZeroMQ socket。

## 安装

CTP Python 库目前面向 Windows 或 Linux x86_64。建议生产环境使用 Linux x86_64。

```bash
cp .env.example .env
# 编辑真实柜台、账号、认证信息和 PostgreSQL DATABASE_URL
uv sync
uv run python src/main.py
```

运行不依赖柜台的测试：

```bash
uv run pytest -q
```

PostgreSQL 集成测试需要一个专用测试数据库：

```bash
TEST_DATABASE_URL=postgresql://ctp_proxy:password@127.0.0.1:5432/ctp_proxy_test \
uv run pytest -m integration -q
```

Docker：

```bash
docker compose up --build -d
docker compose logs -f
```

Compose 在容器内强制使用 `0.0.0.0` 监听 ZeroMQ，使宿主机上的交易引擎可通过
发布的 `5565/5566` 端口连接。镜像同时包含 CTP Linux SDK 在 Apple Silicon
的 `linux/amd64` 模拟环境下所需的 `zh_CN.GB18030` locale。

systemd：

```bash
uv sync
sudo scripts/install_systemd.sh
sudo systemctl enable --now ctp-proxy.service
```

## 配置

主要环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `CTP_MD_BROKER_ID` | 行情经纪商代码；未设置时兼容回退到 `CTP_BROKER_ID` | 必填 |
| `CTP_TD_BROKER_ID` | 交易经纪商代码；未设置时兼容回退到 `CTP_BROKER_ID` | 必填 |
| `CTP_APP_ID` | 交易穿透式监管 AppID | 必填 |
| `CTP_AUTH_CODE` | 交易认证码 | 必填 |
| `CTP_MD_USER_ID` | 行情登录账号 | 必填 |
| `CTP_MD_PASSWORD` | 行情登录密码 | 必填 |
| `CTP_TD_USER_ID` | 交易投资者账号 | 必填 |
| `CTP_TD_PASSWORD` | 交易登录密码 | 必填 |
| `CTP_FRONT_MD` | 行情前置地址 | 必填 |
| `CTP_FRONT_TD` | 交易前置地址 | 必填 |
| `CTP_B_IS_PRODUCTION_MODE` | 传给 CTP MD/TD API 的生产模式标志；兼容旧变量 `CTP_PRODUCTION_MODE` | `true` |
| `CTP_SYMBOLS` | 启动时订阅的合约，逗号分隔 | 空 |
| `ZMQ_PUB_PORT` | 事件发布端口 | `5565` |
| `ZMQ_REP_PORT` | 命令端口 | `5566` |
| `CTP_QUERY_MIN_INTERVAL_SECONDS` | CTP 查询最小间隔 | `1.0` |
| `CTP_SNAPSHOT_TTL_SECONDS` | 查询快照默认有效期 | `5.0` |
| `DATABASE_URL` | PostgreSQL 连接地址 | 必填 |
| `DATABASE_POOL_MIN_SIZE` | 最小数据库连接数 | `1` |
| `DATABASE_POOL_MAX_SIZE` | 最大数据库连接数 | `5` |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | 数据库启动连接超时 | `10` |

MD 与 TD 分别配置 BrokerID、用户和密码；`CTP_APP_ID` 和 `CTP_AUTH_CODE`
只用于 TD 认证。为兼容旧部署，任一专用 BrokerID 未设置时会回退到
`CTP_BROKER_ID`，MD/TD 用户和密码未设置时仍会回退到 `CTP_USER_ID` 和
`CTP_PASSWORD`。订单、成交、账户和持仓消息中的
`account_id` 以及对应 topic 均使用 `CTP_TD_USER_ID`。

Proxy 启动时会连接 PostgreSQL，并自动创建 `ctp_orders` 表和幂等键、CTP 订单 ID 相关索引。数据库不可用时服务启动失败，不会在缺少持久化保护的情况下接受订单。

Docker Compose 会同时启动 PostgreSQL，并将数据保存到 `.env` 中
`POSTGRES_DATA_DIR` 指定的宿主机目录；默认是项目目录下的 `./data`。
部署前必须在 `.env` 中更换示例密码：

```env
POSTGRES_DATA_DIR=./data
POSTGRES_DB=ctp_proxy
POSTGRES_USER=ctp_proxy
POSTGRES_PASSWORD=使用强密码
```

不要将真实 `.env` 提交到 Git。当前协议没有身份认证，生产环境应通过防火墙或私有网络限制两个 ZMQ 端口，尤其是命令端口。

## PUB Topics

```text
marketdata.CTP.<symbol>
orders.<account_id>
orders.<account_id>.<strategy_id>
trades.<account_id>
trades.<account_id>.<strategy_id>
account.<account_id>
positions.<account_id>
status.CTP
errors.CTP
```

消息为两个 frame：第一个 frame 是 topic，第二个 frame 是 JSON：

```json
{
  "schema_version": 1,
  "event": "marketdata",
  "published_at": 1784512345678,
  "data": {}
}
```

## REP Commands

连接 `tcp://<host>:5566`，每次发送一个 JSON 对象。

健康检查：

```json
{"action":"ping"}
```

多策略订阅与退订：

```json
{"action":"subscribe_market_data","client_id":"engine-01","strategy_id":"arb-au","symbols":["au2608","au2610"]}
{"action":"unsubscribe_market_data","client_id":"engine-01","strategy_id":"arb-au","symbols":["au2608"]}
```

查询默认允许返回短时缓存；`force_refresh` 会强制进入 CTP 串行查询队列：

```json
{"action":"get_account","max_age_ms":5000}
{"action":"get_positions","force_refresh":true}
{"action":"get_orders","max_age_ms":5000}
{"action":"get_orders","local_only":true,"strategy_only":true,"strategy_id":"arb-au"}
```

下单：

```json
{
  "action":"place_order",
  "request_id":"request-123",
  "client_id":"engine-01",
  "strategy_id":"arb-au",
  "client_order_id":"arb-au-20260720-000001",
  "symbol":"au2608",
  "exchange":"SHFE",
  "direction":"BUY",
  "offset":"OPEN",
  "price":780.0,
  "volume":1
}
```

三个订单归属字段均为必填。重复提交相同的 `client_id + strategy_id + client_order_id` 不会再次向 CTP 下单。

撤单：

```json
{"action":"cancel_order","client_id":"engine-01","strategy_id":"arb-au","client_order_id":"arb-au-20260720-000001"}
```

Proxy 只在收到该订单的活动状态 `OnRtnOrder` 后接受撤单，并使用 CTP 的
`FrontID + SessionID + OrderRef` 本地三元组定位订单。若订单仍处于
`PENDING_SUBMIT`，客户端应等待 `SUBMITTED` 或 `PARTTRADED` 订单事件后重试。
CTP 回报中的固定宽度 `OrderSysID` 会原样写入 PostgreSQL，包括前导和尾随空格。

所有命令响应采用统一格式：

```json
{
  "schema_version":1,
  "status":"ok",
  "request_id":"request-123",
  "data":{},
  "error":null
}
```

快速发送命令：

```bash
uv run python src/command_example.py '{"action":"ping"}'
```

## 已知边界

- REP 命令接口与 `ibkr-proxy` 保持同类使用方式，命令在 proxy 内串行处理；后续并发量显著增加时可在不改变 JSON 协议的前提下升级成 ROUTER/DEALER。
- PUB/SUB 不保证离线事件补发。订单映射已持久化，但客户端重连后仍应调用账户、持仓和委托查询恢复快照。
- 人工终端或其他 CTP 客户端产生的订单没有 `strategy_id`，只会发布到账户级 topic。
- 当前没有策略级资金、持仓分账，也没有策略级风险额度。


# Docker 相关

如果清华源在你的网络下仍然较慢，可以切换到 USTC：
docker compose build \
  --build-arg DEBIAN_MIRROR=https://mirrors.ustc.edu.cn
也可切回 Debian 官方源：
docker compose build \
  --build-arg DEBIAN_MIRROR=https://deb.debian.org

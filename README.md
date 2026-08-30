# bigqmt-dashboard

大QMT 直连的多账号持仓监控与下单面板。

浏览器里看持仓、资金曲线、成交流水，点一下就把单子报进大QMT —— 没有客户端脚本、没有指令队列、没有轮询延迟。交易通道走 [xtquant_big_convert](https://github.com/litaolemo/xtquant_big_convert)，它把大QMT 内置 Python 包装成 RPC，对外暴露的是 miniQMT 的同名接口。

完整支持可转债：10 张起步的下单规整、0.001 报价精度、溢价率/转股价值/双低、强赎进度与禁买闸门、打新债。

---

## 它和「QMT 面板」通常的做法有什么不同

常见做法是在 QMT 那台机器上跑一个客户端脚本，把持仓推给服务端，顺便从响应里取回服务端排好的指令自己执行。这套东西有几个绕不开的毛病：

- **下单延迟等于客户端轮询周期**，点了「卖出」得等下一轮同步。
- **服务端不知道单子的下场**：报没报进去、被没被废单、什么原因，都发生在客户端里。
- **风控只是建议**：`停止买入` / `锁仓` / `仓位系数` 是下发给客户端的值，客户端不听服务端也没办法。
- **交易逻辑散在客户端**，版本、参数、日志都在别处。

本项目把方向掉过来：服务端主动查、主动报单。

```
                    ┌──────────────────────────────────────────┐
   浏览器 (Vue3)  ──▶│  FastAPI                                 │
                    │   ├─ bridge/  账号 → 连接池、下单、行情    │──RPC──▶ 大QMT 机器 A
                    │   ├─ sync/    轮询拉取 + 实时成交回报      │──RPC──▶ 大QMT 机器 B
                    │   ├─ cb/      可转债参考数据与指标         │
                    │   └─ plugins/ Tushare / MySQL / 代理(可选) │
                    │  SQLite dashboard.db                      │
                    └──────────────────────────────────────────┘
```

| | 推送式 | 本项目（直连） |
|---|---|---|
| 数据来源 | 客户端 push | 服务端主动拉 + 成交实时回报 |
| 下单 | 入队等客户端取 | 当场报单，返回真实 `order_sys_id` |
| 下单失败 | 服务端不可见 | 当场返回原因，并写进审计表 |
| 风控 | 下发给客户端的建议值 | 服务端闸门，判定不过就不会有报单 |
| 挂单视图 | 没有 | `/api/orders` 活动委托 |
| 分钟线回补 | 下发指令→客户端拉→推回来 | 直接 `get_market_data_ex` |
| 在线判定 | 客户端最后一次 push 距今 | 服务端同步状态 + RPC 探活延迟 |

---

## 前置条件

1. **大QMT** 装在一台能开着的机器上（券商版完整客户端，不是极简/miniQMT）。
2. 在大QMT 里跑起 `xtquant_big_convert` 的服务端：按它的
   [部署文档](https://github.com/litaolemo/xtquant_big_convert/blob/main/docs/DEPLOY_QUICKSTART.md)
   建好 `bigqmt_signal_trader_local_config.py`，加载 `BIGQMT_REDIS_DRYRUN.py`。
3. **想真下单，两处开关都要开**：
   - 大QMT 侧 `rpc_allow_order_methods = True`
   - 本项目 `config/accounts.json` 里该账号 `"allow_order": true`

   任一处没开就只有只读链路。建议先只读跑一段时间，把持仓/资产/成交和客户端界面逐字段对上，再开下单。

---

## 安装

```bash
pip install -r requirements.txt
```

```bash
python init_admin.py
```

```bash
python app.py
```

打开 http://localhost:8000

没有任何配置也能启动 —— Tushare、MySQL、大QMT 全部缺失时服务照常跑，只是对应模块降级（下单不可用、题材和市值字段留空）。

---

## 配置

配置文件都在 `config/`，被 `.gitignore` 忽略，仓库里只有 `*.example.json` 模板。环境变量优先级高于配置文件。

### 交易账号（必配，否则无法下单）

`config/accounts.json`（模板见 `config/accounts.example.json`）：

```json
{
  "quote_account_id": "",
  "accounts": [
    {
      "account_id": "你的资金账号",
      "alias": "主号",
      "enabled": true,
      "allow_order": false,
      "timeout_seconds": 6.0,
      "poll_seconds": 4.0,
      "idle_poll_seconds": 60.0,
      "rpc": {
        "transport": "redis",
        "host": "192.168.1.100",
        "port": 6379,
        "db": 5,
        "password": ""
      }
    }
  ]
}
```

- **多账号多实例**：每个账号一条，`rpc` 各配各的，可以连不同机器上的大QMT、不同 Redis db。
- **`rpc` 段整包透传**给桥接层的 `BigQmtRpcClient`，所以换传输只改配置：`"transport": "zmq"` 走同机低延迟，`"transport": "mysql"` 走兼容通道，代码一行不动。
- **`quote_account_id`** 指定哪条连接当行情主连接。行情与账号无关，留空则取第一个启用账号 —— 让 N 个账号各订阅一遍全市场纯属浪费。
- 单账号快速起步也可以只设环境变量：`BIGQMT_ACCOUNT_ID` / `BIGQMT_REDIS_HOST` / `BIGQMT_REDIS_PORT` / `BIGQMT_REDIS_DB` / `BIGQMT_REDIS_PASSWORD`。

### 其余都是可选的

| 配置 | 文件 / 环境变量 | 不配会怎样 |
|---|---|---|
| JWT 密钥 | `SECRET_KEY` | 每次启动随机生成，重启后 JWT 失效。**生产必配。** |
| Tushare | `TUSHARE_TOKEN` 或 `config/tushare.json` | 市值、涨跌幅回退源、K线分析留空 |
| 股票基础库 | `STOCK_DB_*` 或 `config/mysql.json` | 下单搜索只走本地缓存 |
| 记录板同步 | `MYSQL_*` 或 `config/mysql_sync.json` | 研究记录板只存本地 SQLite |
| 东财代理 | `config/proxy.json` | 直连东财；被封时转债比价表补不上转股价 |
| 大模型 | 管理页「大模型配置」 | 记录板退回按分号拆分的规则解析 |

---

## 可转债

转债和股票在下单规则、估值方式、风险点上都不一样，所以单独处理。

**下单规则**（`bridge/instruments.py`）。注意不要复用桥接层的 `code_utils.min_lot()` —— 它对转债返回 100，`(10 // 100) * 100 == 0`，10 张的单子会被规整成 0 直接废掉；它的裸代码判市场按「5/6 开头 = 沪市」，沪市转债 `110xxx` 会被错判到深市。

| | 股票 | 科创板 | ETF/LOF | 可转债 |
|---|---|---|---|---|
| 最小申报 | 100 股 | 200 股 | 100 份 | **10 张** |
| 递增单位 | 100 | **1** | 100 | **10** |
| 报价精度 | 0.01 | 0.01 | 0.001 | **0.001** |
| 交易制度 | T+1 | T+1 | T+1 | **T+0** |

**指标**（`cb/metrics.py`，纯计算，有测试对着真实行情核过）：

```
转股价值 = 100 / 转股价 × 正股价
溢价率   = (债券价 - 转股价值) / 转股价值 × 100%
双低     = 债券价 + 溢价率
强赎进度 = 最近 30 个交易日中「收盘 ≥ 转股价 × 130%」的天数 / 15
```

强赎进度算的是**窗口内的天数**而不是连续天数（条款是「30 个交易日中至少 15 日」）。数据不足时一律判为未触发 —— 宁可漏报，不能拿不完整的数据吓人或误拦单子。

**强赎闸门**：已触发强赎的转债禁止新开仓（强赎公告后按 100 元附近赎回，带溢价买进去是确定性亏损）。判定不了就不拦。

**打新债**：`cb/ipo.py`，交易日开盘后自动申购。**默认 dry-run**，要真申购得在账号配置里显式打开 `"ipo_subscribe": true` + `"ipo_live": true`，并且账号本身 `allow_order`。

**转股价数据**：主源是 akshare `bond_zh_cov`，实测只覆盖约三成的券；缺的用比价表 `bond_cov_comparison` 补（这个接口在机房 IP 上常被东财掐，所以走 `config/proxy.json` 的隧道），再缺就问大QMT 的合约详情要。三层都拿不到时，溢价率相关字段返回 `null`，前端显示「—」并在 tooltip 里说明原因 —— 不会拿缺失的转股价凑一个看起来像模像样的数出来。`GET /api/cb` 的 `coverage` 字段能看到当前覆盖率。

---

## API

### 交易

| 接口 | 说明 |
|---|---|
| `POST /api/position/sell` | 按可用量百分比卖出。100% 走清仓，零股一并卖掉 |
| `POST /api/position/sell_amount` | 按绝对数量卖出，自动按可用量封顶 |
| `POST /api/position/buy` | 按现有持仓百分比加仓（含仓位系数） |
| `POST /api/position/buy_new` | 按绝对数量新开仓，数量校验按品种走 |
| `POST /api/position/sell/cancel` / `buy/cancel` | 撤掉该股票所有可撤的卖单 / 买单 |
| `POST /api/orders/cancel` | 撤单，给 `order_id` 撤一笔，给 `stock_code` 撤该票全部 |
| `POST /api/account/clear-positions` | 一键清仓，逐笔报单并返回每笔结果，需清仓密码 |
| `POST /api/admin/clear-all-positions` | 全账号清仓，仅管理员 |

下单成功返回真实 `order_sys_id`；被风控拦下或大QMT 拒单返回 400 并带原因。批量操作返回每一笔的明细。

### 直连视图

| 接口 | 说明 |
|---|---|
| `GET /api/orders` | 活动委托（默认只看未成交/部成） |
| `GET /api/order-audit` | 下单审计流水：谁、何时、下了什么、成没成、失败原因 |
| `GET /api/accounts/health` | 各账号连接状态、RPC 往返延迟、最近一次同步结果 |
| `GET /api/instrument/{code}` | 品种规则：单位、最小申报量、步进、价格精度、是否 T+0 |

### 可转债

| 接口 | 说明 |
|---|---|
| `GET /api/cb/{bond_code}` | 单券：转股价、转股价值、溢价率、双低、强赎进度 |
| `GET /api/cb` | 参考数据列表 + 转股价覆盖率 |
| `POST /api/cb/refresh` | 手动刷新参考数据（管理员） |
| `GET /api/cb-ipo/pending` | 今日可申购新债 |
| `POST /api/cb-ipo/subscribe` | 手动打新债，默认 dry-run |

### 其余

持仓/资产/曲线/交易日历/研究记录板/用户管理等接口沿用原有形状，`GET /api/data` 的持仓行现在多了 `is_bond` 和 `bond` 字段。

---

## 目录

```
app.py              FastAPI 应用与路由
bridge/             大QMT 直连
  config.py           账号 → 连接参数
  pool.py             account_id → 连接实例、探活
  orders.py           唯一下单出口，风控闸门在这里
  market.py           行情（全局共用一条连接）
  instruments.py      品种识别与下单规整
sync/               账户数据同步
  poller.py           按账号轮询拉取
  callbacks.py        大QMT 推来的实时委托/成交回报
  adapters.py         桥接层对象 → 落库格式
cb/                 可转债
  reference.py        转股价/正股/申购信息（日更缓存）
  metrics.py          溢价率/转股价值/双低/强赎（纯计算）
  service.py          参考数据 + 实时行情 + 指标
  ipo.py              打新债
plugins/            可选外部数据源，缺配置自动降级
tools/              开发用脚本（造数据、查库）
```

---

## 测试

```bash
python -m pytest tests/ -q
```

不需要 QMT、不需要任何密钥。下单链路用 `tests/fake_bridge.py` 的测试替身：风控闸门、数量规整、审计落库都跑真实代码，只有最后那一步 RPC 是假的。

---

## 尚未包含

**做T执行引擎。** 面板上的做T开关现在只写服务端状态（`t0_status` 表），`GET /api/t0-scan` 能算出机会行，但**没有自动执行**。原来的择时逻辑跑在 QMT 端脚本里，不在本仓库，直连后这段没有归宿。要恢复自动做T，需要把择时规则（B 点算法、回转比例、当日次数上限、冷却时间）实现进 `t0/engine.py`。

---

## 安全

- 仓库里没有任何密钥。所有凭证走 `config/*.json`（已 gitignore）或环境变量。
- `SECRET_KEY` 没有可用的默认值 —— 不设置就每进程随机生成。
- 下单默认关闭，且需要大QMT 侧和本项目两处同时打开。
- 清仓、全账号清仓需要独立的清仓密码。
- 展示模式（管理员全局开关）下服务端拒绝一切报单，面板变为只读。

## License

MIT

# 本地量化交易系统

自底向上分五层搭建，**五层全部完成并通过正式验收**（2026-08-30）。

```
┌─────────────────────────────────────────┐
│  第5层：调度与监控层（数据巡检、流水线、定时） │  ✅ 完成（含失败告警/时区修复）
├─────────────────────────────────────────┤
│  第4层：风控层（仓位约束、止损、组合熔断）    │  ✅ 完成（T+1语义/熔断状态机已验证）
├─────────────────────────────────────────┤
│  第3层：策略层（五因子、Top50、组合回测）    │  ✅ 完成（含未来函数硬拦截）
├─────────────────────────────────────────┤
│  第2层：回测引擎（向量化撮合、完整成本）      │  ✅ 完成（成本读 costs.yaml 单一源）
├─────────────────────────────────────────┤
│  第1层：数据层（获取、清洗、存储、更新）      │  ✅ 完成（全市场 A 股，含北交所）
└─────────────────────────────────────────┘
```

**验收摘要（2026-08-31 终版 v4：walk-forward 三 bug 修复后，正式红灯）**：
- **【红灯】walk-forward（scripts/walk_forward.py，训练 24 月/测试 12 月单年隔离）**
  生产权重(0.44/0.31/0.25) 7 个测试年：3 正 4 负（2019 +23%/2020 +14%/2021 +25%，
  2017 -15%/2018 -29%/2022 -17%/2023 -6%），**WF 总 -17.2% / 夏普 -0.236 / 回撤 45%**
  ——生产权重外推是负 alpha。滚动 IC 定权版 WF 总 -13.5% / 夏普 -0.197，同样失败。
- 此前版本 walk_forward 有 3 个致命 bug 已修：①权重键带前缀导致 lookup miss、
  实际跑的是等权（定权是假象）；②测试期未截上界，每 fold 是"当年选股持有到
  2023"的混合体；③拼接时间轴重叠。修复后数字以本节为准。
- 生产"样本外 2019-2023 +28.3%"是看完全部数据后手工调权的拟合数字，
  与 walk-forward 负 alpha 不可能同时为真，已作废。
- 结论：因子组合无可外推 alpha，回因子层重来；walk-forward 为唯一验收标准。
- 成本模型未实证校准（滑点 ±100% → 收益 10.7%~51%），任何数字按区间理解。
- 因子池：turnover/reversal/value 两段 IC 同向（保留）；profitability 两段反向
  （区间噪音，已踢）；momentum IC 弱负（权重 0）。
- 风控：个股止损/组合熔断两段样本外均负贡献，全部禁用（仅存档）。
- 调仓次数 = 计划信号数；factor_ic 产物名含区间标注（如 factor_ic_2015-2018_*.csv）。

---

## 一、环境

| 项目 | 值 | 说明 |
|---|---|---|
| 项目路径 | `D:\量化交易` | |
| Python | 3.13.14（venv：`C:\Users\Administrator\.workbuddy\binaries\python\envs\quant313`） | 运行环境由 WorkBuddy 托管 |
| MySQL | 8.0.46，路径 `E:\Marvis App\MySQL`，端口 3306，服务运行中 | |
| 核心依赖 | akshare 1.18.94 / pandas 3.0.5 / numpy 2.5.2 / SQLAlchemy 2.0.52 / backtrader 1.9.78.123 | |

---

## 二、第 1 层：数据层

### 2.1 数据源实测结论（2026-08-30 在本机实测）

⚠️ **重要发现：东方财富历史 K 线接口在本机不可用。**
其历史域名 `push2his.eastmoney.com` 连接被重置（HTTP 000），而实时域名 `push2` 正常。
因此 `stock_zh_a_hist`、`index_zh_a_hist`、`stock_zh_a_st_em`、`stock_board_industry_*_em`
这一批东财接口全部无法使用。**本项目已改用新浪作为主力数据源。**

| 用途 | 接口 | 实测状态 | 说明 |
|---|---|---|---|
| 股票列表 | `stock_info_a_code_name` | ✅ 5551 只 | 全 A |
| 沪市/深市/北交所 | `stock_info_{sh,sz,bj}_name_code` | ✅ | 含上市日期；深市/北交所还含所属行业 |
| **沪市退市** | `stock_info_sh_delist` | ✅ 159 只 | 新版 akshare 已移除旧的 `stock_info_delist` |
| **深市退市** | `stock_info_sz_delist` | ✅ 208 只 | 同上 |
| **日线（主力）** | `stock_zh_a_daily`（新浪） | ✅ | 一次返回全历史，且**带流通股本历史序列** |
| 日线（备选） | `stock_zh_a_hist_tx`（腾讯） | ✅ | 按日期区间返回 |
| 日线（不可用） | `stock_zh_a_hist`（东财） | ❌ | 域名不可达 |
| 指数日线 | `stock_zh_index_daily`（新浪） | ✅ | 沪深300 = `sh000300` |
| 交易日历 | `tool_trade_date_hist_sina` | ✅ 8797 天 | |
| 财务指标 | `stock_financial_analysis_indicator` | ✅ | 同花顺，按报告期 |
| 申万行业分类 | 直连 `swsresearch.com` xls | ✅ 12905 行 | 带「计入日期」，可做时点对齐 |
| ST 名单 | `stock_zh_a_st_em` | ❌ | 东财不可用；**已用股票名称推断兜底**（330 只） |

### 2.2 三个关键设计决策

**1）新浪数据接口的字段口径（实测确认，别搞错）**
- `turnover` 是**比率不是百分数**：0.002854 表示 0.2854%，入库时 ×100。
- `outstanding_share` 是**真实的流通股本历史序列**（浦发银行 2000 年 4 亿股 → 2023 年 293 亿股），
  因此可以自己算流通市值，**不依赖任何估值接口**。这是选新浪而非东财的主因。

**2）三份复权价各司其职**
| 类型 | 用途 | 特性 |
|---|---|---|
| `qfq` 前复权 | 因子与信号计算 | 以最新价为基准，**历史会被追溯修改** |
| `hfq` 后复权 | 收益计算 | 以上市首日为基准，历史不变，结果可复现 |
| `none` 不复权 | 市值、涨跌停、1 元退市判断 | 真实成交价 |

因为 qfq 的历史会被分红送股改写，增量更新时 `detect_restatement()` 会检测复权基准是否漂移，
漂移了就重拉该股全历史 —— 否则回测曲线会在除权日出现假跳空。

**3）估值因子不需要总股本**
`EP = 净利润/总市值 = (净利润/股本)/(市值/股本) = EPS/股价`，分子分母的股本约掉了。
所以算 EP/BP 不需要总股本、不需要市值接口，数据源依赖最少。

---

## 三、目录结构

```
D:\量化交易
├── config/                     # 全部配置，YAML + 环境变量
│   ├── database.yaml           # 数据库连接（密码走 .env）
│   ├── universe.yaml           # 股票池、数据范围 2015起、基准沪深300
│   ├── factors.yaml            # ★ 因子研究标准化工作流（YAML 驱动）
│   ├── strategy.yaml           # 月度调仓 / Top50 / 流通市值加权
│   ├── costs.yaml              # 印花税、佣金、流动性滑点
│   └── risk.yaml               # 第4层风控参数
│
├── sql/
│   ├── 01_create_database.sql
│   ├── 02_create_tables.sql    # 11 张表，日线按年分区
│   └── 03_create_views.sql     # 常用视图 + 数据质量体检视图
│
├── src/
│   ├── common/
│   │   ├── config.py           # YAML 加载 + ${ENV} 插值 + 校验
│   │   ├── db.py               # SQLAlchemy 封装（建库/批量写/分块）
│   │   ├── logger.py           # loguru：控制台 + 按天切分文件
│   │   ├── pit.py              # ★ Point-In-Time 时点对齐
│   │   └── utils.py            # 日期、代码标准化、分块、重试
│   │
│   ├── layer1_data/            # ✅ 第 1 层
│   │   ├── fetcher/
│   │   │   ├── akshare_client.py    # 限流 + 指数退避重试 + 缓存 + 失败清单
│   │   │   ├── stock_list.py        # 股票池（含退市 361 只）
│   │   │   ├── daily_price.py       # 日线三复权 + 流通市值派生
│   │   │   ├── financial.py         # 财务 + TTM + 公告可用日
│   │   │   └── industry.py          # 申万行业分类（含历史变动）
│   │   ├── cleaner/
│   │   │   ├── price_cleaner.py     # 结构校验/停牌/涨跌停/复权漂移检测
│   │   │   └── financial_cleaner.py # 越界置NaN/去重/勾稽检查
│   │   ├── storage/
│   │   │   ├── repository.py        # 列对齐 + 幂等写入 + 更新日志
│   │   │   └── updater.py           # 增量更新编排
│   │   └── ...
│   │
│   ├── layer2_backtest/        # 第 2 层（成本/绩效已完成，引擎待建）
│   ├── layer3_strategy/        # 第 3 层（因子/预处理已完成）
│   ├── layer4_risk/            # 第 4 层（待建）
│   └── layer5_scheduler/       # 第 5 层（待建）
│
├── scripts/
│   ├── init_db.py              # 建库建表
│   ├── update_data.py          # 数据更新入口（全量/增量/小样本）
│   ├── check_data_layer.py     # ★ 数据层自检（无需数据库）
│   └── probe_akshare.py        # AKShare 接口可用性探测
│
├── data/                       # 缓存与中间产物（不入库）
├── logs/
├── outputs/
└── .env                        # ← 数据库密码填这里（已在 .gitignore）
```

---

## 四、数据库表设计（11 张）

| 表名 | 说明 | 关键点 |
|---|---|---|
| `stock_basic` | 股票主表 | **含退市股**，消除幸存者偏差 |
| `daily_price` | 日线行情 | 按年分区；主键 `(trade_date, ts_code, adj_type)` |
| `daily_basic` | 每日市值/估值 | 按年分区；流通市值 = 不复权收盘价 × 流通股本 |
| `financial_indicator` | 财务指标 | **同时存 report_date 与 ann_date** |
| `industry_classification` | 申万行业 | 带 effective_date，支持行业变更历史 |
| `index_daily` | 指数日线 | 基准沪深300 |
| `trade_calendar` | 交易日历 | 含月末标记（调仓用） |
| `adj_factor` | 复权因子 | |
| `update_log` | 更新日志 | 每只股票的抓取行数/耗时/状态 |
| `factor_exposure` | 因子暴露 | 第 3 层产出 |
| `backtest_result` | 回测结果 | 含参数快照 JSON |

**为什么日线表主键不用自增 id**：MySQL 要求分区列必须出现在所有唯一键中，自增 id 做不到；
而 `(trade_date, ts_code, adj_type)` 天然唯一，可直接 `INSERT IGNORE` 实现幂等增量写入。

---

## 五、快速开始

### 第 1 步：填数据库密码
编辑 `D:\量化交易\.env`，把 `DB_PASSWORD=` 后面填上你的 MySQL root 密码。
（注意：注释必须独占一行，不要写成 `DB_PASSWORD=xxx  # 注释`，dotenv 会把整行都当值。）

### 第 2 步：建库建表
```bash
cd /d/量化交易
python scripts/init_db.py
```

### 第 3 步：数据层自检（无需数据库）
```bash
python scripts/check_data_layer.py
```

### 第 4 步：小样本跑通（建议先跑这个）
```bash
python scripts/update_data.py --sample 30 --start 2023-01-01
```

### 第 5 步：全量抓取（5000+ 只，耗时长，建议后台）
```bash
python scripts/update_data.py --all --start 2015-01-01
```

### 日常增量更新
```bash
python scripts/update_data.py --update
```

---

## 六、已知限制

1. **东财系接口在本机全部不可用**（历史 K 线域名被重置），已改用新浪。若网络环境变化，
   把 `stock_zh_a_hist` 加回 `src/layer1_data/fetcher/daily_price.py` 的 `SOURCES` 即可。
2. **ST 名单接口不可用**，改用股票名称含 "ST" 推断（当前 330 只）。这是"当前状态"，
   历史 ST 状态在清洗层通过涨跌幅限制（5%）反推。
3. 财务接口（同花顺）**不提供净利润/总资产等绝对额**，只提供每股指标与比率。
   本项目因子用 EP/BP/ROE，均可由每股指标算出，不受影响。注意该接口高频调用会被限流
   （响应挂起不报错），`update_financial` 已实现分批入库（每 500 只一存），卡死最多丢一批。
4. 申万行业**名称对照表**官网未公开（候选 URL 均 404），行业以代码分组（L1 共 38 组），
   名称形如 `SW_L1_44`。中性化只需要分组标识，不影响功能。
5. **B 股（200/900）明确排除**：港币/美元计价，A 股策略口径不含（非数据源问题）。
   北交所（43/83/87/92）已纳入抓取，新浪 `stock_zh_a_daily` 以 bj 前缀支持。
6. **财务数据覆盖**：当前 financial_indicator 仅覆盖部分股票（同花顺分批抓取中），
   估值/盈利质量因子在补全前近乎中性；补全后重跑 `run_top50_strategy.py` 为完整五因子版。

## 七、运行命令速查

```bash
# 数据更新（日线增量；--force-full 全量重写）
python scripts/update_data.py --only daily --start 2015-01-01 --workers 12
python scripts/update_data.py --only financial          # 财务（分批入库）

# 正式验收三件套
python scripts/run_top50_strategy.py                    # Top50 回测（默认 2019-2023）
python scripts/factor_evaluation.py                     # 因子 IC/分层检验
python scripts/example_risk.py                          # 风控报告

# 日常流水线 / 定时调度
python scripts/run_daily.py --steps monitor             # 单环节巡检
python scripts/run_daily.py --daemon                    # 常驻调度（读 config/scheduler.yaml）

# ---------------- Web 前端 / 桌面 App ----------------
python web/app.py                                      # 网页模式：http://localhost:8000
python web/desktop.py                                  # 桌面 App 模式（PyWebview 原生窗口）
python build_exe.py                                    # 打包 Windows exe → dist/QuantDesktop/
```

## 八、Web 前端与桌面 App

前端是 **FastAPI + Vue3 + ECharts**（深色主题、涨红跌绿），展示五层系统的全部产出：
总览看板（净值曲线/风控对比）、回测明细、因子 IC/分层检验、风控事件与数据健康监控。
风控参数可在页面上调整并一键重跑。

- **网页模式**：`python web/app.py` → 浏览器打开 http://localhost:8000
- **桌面 App**：`python web/desktop.py`（原生窗口，Windows 用系统 WebView2）；
  打包 `python build_exe.py` → `dist/QuantDesktop/QuantDesktop.exe` 双击即用
- 前端资源（Vue/ECharts）已本地化到 `web/static/vendor/`，**离线可用**
- 数据源为本机 MySQL（需 MySQL 服务运行中），前端只读，调参重跑走后台任务

> 说明：桌面 App 依赖本机 MySQL 数据；产出目录 `outputs/` 不入库（见 .gitignore），
> 克隆后需先跑 `scripts/update_data.py` 拉取数据再启动前端。



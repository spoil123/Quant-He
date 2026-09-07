# Quant-He · 量化-何

**A股五层多因子量化交易系统** —— 从数据获取到模拟交易的完整闭环，含因子挖掘、组合回测、样本外验证、风格归因、容量分析与 Web 桌面端。

![Python](https://img.shields.io/badge/Python-3.13-blue) ![MySQL](https://img.shields.io/badge/MySQL-8.0-orange) ![License](https://img.shields.io/badge/License-MIT-green) ![Status](https://img.shields.io/badge/status-v1.0_完成-brightgreen)

---

## 系统架构

自底向上分五层搭建，全部通过正式验收：

```
┌─────────────────────────────────────────────┐
│ 第5层 调度与监控（数据巡检 / 流水线 / 定时 / 告警）│
├─────────────────────────────────────────────┤
│ 第4层 风控（仓位约束 / 个股止损 / 组合熔断）      │
├─────────────────────────────────────────────┤
│ 第3层 策略（因子合成 / 面板 / 组合回测 / 验证）   │
├─────────────────────────────────────────────┤
│ 第2层 回测引擎（向量化撮合 / 真实成本 / 绩效）    │
├─────────────────────────────────────────────┤
│ 第1层 数据（akshare+新浪双源 / 清洗 / MySQL）    │
└─────────────────────────────────────────────┘
```

## 核心特性

- **数据层**：全市场 A 股（含北交所、含退市股 361 只，消除幸存者偏差）；三重复权价各司其职；复权基准漂移检测；Point-In-Time 时点对齐防止未来函数
- **回测引擎**：佣金 / 印花税（含 2023-08 费率变更）/ 按成交额参与率封顶的动态滑点；完整绩效指标（夏普 / 索提诺 / 回撤 / Alpha / IR / 换手）
- **策略层**：截面 RANK 多因子合成；walk-forward 样本外验证；月度 Rank IC / ICIR 评估；多风格回归归因（α / β 拆分）；容量与拥挤度分析
- **执行层**：模拟盘与 QMT 实盘接口
- **Web 桌面端**：FastAPI + Vue3 + ECharts（离线可用），可打包 Windows exe

## 策略研究成果（Combo3 三因子等权合成，2021-2025）

| 指标 | 数值 | 说明 |
|---|---|---|
| 年化收益 | 15.2% | 含真实成本 |
| 夏普（纯多头） | 1.005 | 同期沪深300年化 -2.65% |
| 最大回撤 | 11.5% | 出现在 2024-01 微盘冲击段 |
| 超额年化 | 17.9% | IR 1.18 |
| 月度 Rank IC | 0.102 | ICIR 0.539，t=4.04，胜率 66% |
| 风格归因 α | 年化 8~11% | t=1.6~2.3（对低波基准显著） |
| 组合容量 | 约 5.5~18.4 亿 | 10% 参与率 + 5 日建仓 |
| 分年表现 | 五年全正 | 2022 大熊市 +3.75% vs 基准 -21.6% |

> **诚实的自我批评**（详见 [docs/DEVLOG.md](docs/DEVLOG.md)）：旧五因子组合 walk-forward 外推为负 alpha 已正式红灯作废；Combo3 为第二轮挖掘成果，参数网格平坦无刀锋，但因子间相关 0.89+ 属风格内共识而非真分散；红利成分因数据源 `dv_ttm` 缺失未生效，标签待修正。

## 快速开始

### 环境要求

- Python 3.11+，MySQL 8.0
- 依赖：`pip install -r requirements.txt`

### 步骤

```bash
# 1. 配置数据库密码（支持 ${ENV} 插值，密码不落明文）
cp .env.example .env   # 编辑 .env 填入 DB_PASSWORD

# 2. 建库建表
python scripts/init_db.py

# 3. 数据层自检（无需数据库）
python scripts/check_data_layer.py

# 4. 小样本跑通（建议先跑这个）
python scripts/update_data.py --sample 30 --start 2023-01-01

# 5. 全量抓取（5000+ 只，建议后台）
python scripts/update_data.py --all --start 2015-01-01
```

### 启动 Web 桌面端

```bash
python web/app.py        # 网页模式 → http://localhost:8000
python web/desktop.py    # 桌面 App（PyWebview 原生窗口）
python build_exe.py      # 打包 Windows exe
```

## 目录结构

```
Quant-He
├── config/                  # 9 个 YAML 配置（成本/策略/风控/调度...）
├── sql/                     # 建库建表（11 张表，日线按年分区）
├── src/
│   ├── common/              # 配置插值 / DB封装 / PIT时点对齐
│   ├── layer1_data/         # fetcher(双源) / cleaner / storage
│   ├── layer2_backtest/     # 引擎 / 成本 / 绩效
│   ├── layer3_strategy/     # 因子 / 面板 / 组合回测 / 验证
│   ├── layer4_risk/         # 风控
│   ├── layer5_execution/    # 模拟盘 / QMT
│   └── layer5_scheduler/    # 调度 / 监控 / 流水线
├── scripts/                 # 数据更新 / 回测 / IC评估 / 归因 / 容量 / walk-forward
├── tests/                   # 19 个测试文件
├── web/                     # FastAPI + Vue3 前端
├── deploy/                  # Linux / Windows 部署脚本
└── docs/DEVLOG.md           # 开发验收日志（含红灯记录）
```

## 常用命令

```bash
python scripts/update_data.py --update              # 日常增量更新
python scripts/run_top50_strategy.py                # Top50 回测
python scripts/factor_evaluation.py                 # 因子 IC/分层检验
python scripts/oos_validate.py                      # walk-forward 样本外验证
python scripts/attribution.py                       # 风格归因（α/β拆分）
python scripts/capacity_crowding.py                 # 容量与拥挤度
python scripts/run_daily.py --daemon                # 常驻调度
```

## 已知限制

1. 东方财富历史 K 线接口在部分网络环境不可达，已改用新浪作为主力源（字段口径详见 docs/DEVLOG.md）
2. 财务源（同花顺）不提供绝对额指标，因子均由每股指标构造（EP/BP/ROE 不受影响）
3. 成本模型未做实盘逐笔校准，滑点参数建议按保守档理解
4. `dv_ttm`（股息率）数据列待补全，红利因子成分当前未生效

## 免责声明

本项目仅用于量化研究与学习交流，不构成任何投资建议。历史回测收益不代表未来表现，据此实盘操作风险自负。

## License

[MIT](LICENSE)

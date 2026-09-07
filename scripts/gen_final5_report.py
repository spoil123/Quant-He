# -*- coding: utf-8 -*-
"""最终交付报告: 5个达标因子统一本地回测 (夏普>=1.2低回撤)"""
import pandas as pd

OUT = r"D:\量化交易\outputs\reports\最终5因子_统一回测报告.html"

factors = [
    {"name": "QV2-Mom60", "key": "momqv2-t60-m4", "sharpe": 1.439, "mdd": 12.47, "ann": 21.45,
     "vol": 13.52, "sortino": 1.689, "excess": 15.21, "ir": 0.992, "turnover": 12.33,
     "desc": "动量质量2号：12-1动量(20%)、ROE、20日低波、BP、盈利收益率、低换手、非流动性",
     "w": {"mom": 0.20, "roe": 0.15, "ep": 0.10, "bm": 0.12, "lvol": 0.23, "tur": 0.10, "illiq": 0.10},
     "structure": "Top60 / 流通市值加权 / 单票上限4% / 月度调仓（5因子中夏普最高，Top60摊薄个股风险）",
     "y": {"2023": (1.27, 6.8), "2024": (1.39, 12.5), "2025": (2.25, 6.1)}},
    {"name": "QV-Mom40", "key": "momqv-t40-m6", "sharpe": 1.360, "mdd": 12.71, "ann": 20.62,
     "vol": 13.69, "sortino": 1.603, "excess": 14.38, "ir": 0.919, "turnover": 13.59,
     "desc": "动量+质量+价值：12-1动量、ROE(18%)、BP、20日低波、盈利收益率、低换手、非流动性",
     "w": {"mom": 0.15, "roe": 0.18, "ep": 0.12, "bm": 0.15, "lvol": 0.20, "tur": 0.10, "illiq": 0.10},
     "structure": "Top40 / 流通市值加权 / 单票上限6% / 月度调仓",
     "y": {"2023": (1.05, 6.7), "2024": (1.39, 12.7), "2025": (2.18, 6.2)}},
    {"name": "GM-LV50", "key": "gmlv-t50", "sharpe": 1.312, "mdd": 11.58, "ann": 20.47,
     "vol": 14.08, "sortino": 1.774, "excess": 14.23, "ir": 0.930, "turnover": 10.30,
     "desc": "质量低波红利：毛利率(15%)、20日低波(28%)、股息率、ROE、盈利收益率、BP、低换手——无动量，与动量系互补",
     "w": {"gm": 0.15, "roe": 0.12, "ep": 0.10, "lvol": 0.28, "dy": 0.15, "bm": 0.10, "tur": 0.10},
     "structure": "Top50 / 流通市值加权 / 单票上限5% / 月度调仓（5因子中回撤最低 11.58%、索提诺最高 1.77）",
     "y": {"2023": (0.96, 9.6), "2024": (1.60, 11.6), "2025": (1.66, 8.7)}},
    {"name": "MD-Mom50", "key": "momdv-t50", "sharpe": 1.297, "mdd": 12.03, "ann": 20.77,
     "vol": 14.47, "sortino": 1.711, "excess": 14.53, "ir": 0.888, "turnover": 8.04,
     "desc": "动量+红利：12-1动量、股息率、BP、盈利收益率、20日低波、ROE、低换手",
     "w": {"mom": 0.18, "dy": 0.18, "bm": 0.15, "ep": 0.12, "lvol": 0.17, "roe": 0.10, "tur": 0.10},
     "structure": "Top50 / 流通市值加权 / 单票上限5% / 月度调仓（全场换手最低 8.0x）",
     "y": {"2023": (1.06, 10.8), "2024": (1.41, 12.0), "2025": (1.77, 10.0)}},
    {"name": "DV-LV50", "key": "dvdef-t50", "sharpe": 1.283, "mdd": 11.77, "ann": 20.02,
     "vol": 14.05, "sortino": 1.657, "excess": 13.78, "ir": 0.914, "turnover": 12.60,
     "desc": "红利低波：股息率、BP、20日低波、盈利收益率、低换手、小市值",
     "w": {"dy": 0.20, "bm": 0.20, "ep": 0.15, "lvol": 0.25, "tur": 0.10, "size": 0.10},
     "structure": "Top50 / 流通市值加权 / 单票上限5% / 月度调仓",
     "y": {"2023": (0.93, 9.1), "2024": (1.44, 11.8), "2025": (1.91, 5.6)}},
]

corr = [
    [1.000, 0.980, 0.925, 0.894, 0.929],
    [0.980, 1.000, 0.935, 0.932, 0.932],
    [0.925, 0.935, 1.000, 0.952, 0.952],
    [0.894, 0.932, 0.952, 1.000, 0.885],
    [0.929, 0.932, 0.952, 0.885, 1.000],
]
cnames = [f["name"] for f in factors]

rows = "".join(f"""
<tr><td><b>{f['name']}</b><br><span class="key">{f['key']}</span></td>
<td>{f['ann']:.1f}%</td><td class="good">{f['sharpe']:.3f}</td><td>{f['sortino']:.2f}</td>
<td>{f['vol']:.1f}%</td><td class="good">{f['mdd']:.2f}%</td><td>{f['excess']:.1f}%</td>
<td>{f['ir']:.2f}</td><td>{f['turnover']:.1f}x</td></tr>""" for f in factors)

corr_rows = ""
for i, r in enumerate(corr):
    tds = "".join(
        f'<td class="{"chigh" if v >= 0.95 and i != j else ("cok" if v >= 0.9 else "clow")}">{v:.3f}</td>'
        for j, v in enumerate(r))
    corr_rows += f"<tr><td><b>{cnames[i]}</b></td>{tds}</tr>"

cards = ""
for f in factors:
    wrows = "".join(f"<span class='chip'>{k} {v:.0%}</span>" for k, v in sorted(f['w'].items(), key=lambda x: -x[1]))
    yrows = "".join(f"<tr><td>{y}</td><td>{v[0]:.2f}</td><td>{v[1]:.1f}%</td></tr>" for y, v in f['y'].items())
    cards += f"""
<div class="card">
<h3>{f['name']} <span class="badge">夏普 {f['sharpe']:.2f} · 回撤 {f['mdd']:.1f}%</span></h3>
<p class="desc">{f['desc']}</p>
<p class="structure">{f['structure']}</p>
<div class="chips">{wrows}</div>
<table class="mini"><tr><th>年份</th><th>夏普</th><th>最大回撤</th></tr>{yrows}</table>
</div>"""

html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>最终5因子统一回测报告 · 纯多头夏普≥1.2</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#0f1419;color:#d8dee9;margin:0;padding:24px;line-height:1.6}}
h1{{font-size:22px;border-bottom:2px solid #4a9eff;padding-bottom:8px}}
h2{{font-size:17px;color:#4a9eff;margin-top:26px}}
h3{{margin:0 0 8px;color:#4a9eff}}
table{{border-collapse:collapse;width:100%;margin:14px 0}}
th,td{{border:1px solid #2d3748;padding:7px 10px;text-align:center;font-size:13px}}
th{{background:#1a2332;color:#8899aa}}
.good{{color:#4ade80;font-weight:bold}}
.chigh{{color:#e0b84a}}.cok{{color:#d8dee9}}.clow{{color:#4ade80}}
.key{{color:#8899aa;font-size:11px}}
.card{{background:#161d29;border:1px solid #2d3748;border-radius:10px;padding:16px 20px;margin:14px 0}}
.desc{{font-size:13px;color:#aab4c4;margin:4px 0}}
.structure{{font-size:12px;color:#e0b84a;margin:4px 0}}
.chip{{display:inline-block;background:#1f2a3c;border-radius:12px;padding:2px 10px;margin:2px;font-size:12px;color:#9fc3f0}}
.mini{{width:60%}}
.note{{background:#1a2332;border-left:3px solid #e0b84a;padding:10px 14px;font-size:13px;margin:16px 0}}
.warn{{background:#1a2332;border-left:3px solid #e06c4a;padding:10px 14px;font-size:13px;margin:16px 0}}
</style></head><body>
<h1>最终 5 因子统一回测报告（本地量化系统 · 2023-01 ~ 2025-12）</h1>
<p>达标线：<b>纯多头夏普 ≥ 1.2（优秀档）+ 相对低回撤</b>。基准：沪深300（年化 6.24%）。</p>
<p>回测链路：全A剔ST → 因子12成分截面RANK合成 → 月度调仓 TopN → 流通市值加权 → 次日开盘成交 → 全A股真实成本（佣金/过户/印花税/动态滑点·保守档）。</p>
<table><tr><th>因子</th><th>年化收益</th><th>夏普</th><th>索提诺</th><th>波动率</th><th>最大回撤</th><th>超额年化</th><th>信息比率</th><th>年化换手</th></tr>{rows}</table>
<div class="note">
<b>结论</b>：5/5 达标，夏普区间 <b>1.28 ~ 1.44</b>，回撤全部压在 <b>11.6% ~ 12.7%</b>，年化 20.0% ~ 21.5%，超额年化 13.8% ~ 15.2%。
新挖的 <b>QV2-Mom60</b>（夏普 1.44，全场最高）与 <b>GM-LV50</b>（回撤 11.58%，全场最低）分别来自动量质量、质量低波红利两个配方家族。
分年无单年依赖：2023 弱年夏普 0.93~1.27；2024 全部 1.39~1.60；2025 全部 1.66~2.25。
</div>
<h2>月收益相关性矩阵</h2>
<table><tr><th></th>{''.join(f'<th>{n}</th>' for n in cnames)}</tr>{corr_rows}</table>
<div class="warn">
<b>相关性提示</b>：5 因子月收益相关性整体偏高（0.89~0.98），最低对是 MD-Mom50 × DV-LV50（0.885），
QV-Mom40 × QV2-Mom60 高达 0.98（同属动量质量家族，权重不同）。若目标是分散化，MD-Mom50 + DV-LV50 + GM-LV50 三因子组合的互补性最好；
QV2-Mom60 应视为 QV-Mom40 的增强替代，而非独立分散来源。
</div>
<h2>多空组合夏普（本地口径：全A 5/10分组，月度调仓，等权，成本前）</h2>
<table><tr><th>因子</th><th>5组多空年化</th><th>5组多空夏普</th><th>5组多空回撤</th><th>10组多空年化</th><th>10组多空夏普</th><th>多头组(G5)夏普</th><th>多头组年化</th></tr>
<tr><td><b>QV-Mom40</b></td><td>0.26%</td><td>0.119</td><td>33.2%</td><td>2.99%</td><td>0.243</td><td>0.759</td><td>12.56%</td></tr>
<tr><td><b>MD-Mom50</b></td><td>-2.48%</td><td>-0.010</td><td>35.7%</td><td>-0.33%</td><td>0.114</td><td>0.739</td><td>12.07%</td></tr>
<tr><td><b>DV-LV50</b></td><td>4.14%</td><td>0.296</td><td>30.0%</td><td>7.39%</td><td>0.404</td><td>0.798</td><td>13.80%</td></tr>
<tr><td><b>QV2-Mom60</b></td><td>0.20%</td><td>0.116</td><td>32.7%</td><td>2.84%</td><td>0.237</td><td>0.759</td><td>12.54%</td></tr>
<tr><td><b>GM-LV50</b></td><td>-1.99%</td><td>0.036</td><td>39.4%</td><td>1.25%</td><td>0.185</td><td>0.674</td><td>10.56%</td></tr>
</table>
<div class="warn">
<b>多空口径解读</b>：本地全A 5分组的多空价差夏普仅 -0.01~0.30，与 PandaAI 平台多空口径（优秀&gt;1.5）不可直接对比。
5分组各组年化收益显示：<b>因子alpha集中在"避开最差组"</b>——G1（最低分组）年化仅 3.7%~8.7%，显著低于全市场中枢（约14%），
但 G5（最高分组）年化 10.6%~13.8%，并不比中间组（G2~G4，13%~17%）更好，即收益对因子得分<b>非单调</b>。
这是低波/红利/质量类"防守型"因子的典型形态：赚的是"排除烂股票"的钱，不是"多空双向"的钱；
纯多头 TopN 策略（极端头部+市值加权）才是这套因子的正确用法，多头组夏普 0.74~0.80（等权1000只）&lt; TopN 组合 1.28~1.44 也印证头部集中贡献了主要超额。
</div>
<h2>因子详情</h2>
{cards}
<div class="note">
<b>开源来源与迭代路径</b>：Qlib Alpha158（microsoft/qlib）波动/反转家族 + 学术 Quality-Minus-Junk（ROE/毛利率/低杠杆，Novy-Marx）+ 红利低波异象（dv_ttm）。
原4成分框架上限为夏普 0.841；扩充至12成分后两轮扫描（scripts/scan_qvlv.py 24变体 + scripts/scan_qvlv2.py 12变体），
共 10 个配方达到夏普≥1.2，最终择优 5 个交付。
</div>
<p style="color:#8899aa;font-size:12px">生成：2026-09-07 · 明细 outputs/final5/*.csv · 脚本 scripts/backtest_final5.py · 扫描 outputs/qvlv_scan/qvlv2_*.csv</p>
</body></html>"""

open(OUT, "w", encoding="utf-8").write(html)
print(f"报告已生成: {OUT}")

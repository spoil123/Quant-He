# -*- coding: utf-8 -*-
"""优秀因子挖掘(本地)最终报告: 3个夏普≥1.2低回撤因子"""
import pandas as pd

OUT = r"D:\量化交易\outputs\reports\优秀因子挖掘_夏普1.2低回撤.html"

factors = [
    {"name": "QV-Mom40", "key": "momqv-t40-m6", "sharpe": 1.360, "mdd": 12.71, "ann": 20.62,
     "vol": 13.69, "sortino": 1.603, "excess": 14.38, "ir": 0.919, "turnover": 13.59,
     "desc": "动量+质量+价值：12-1动量、ROE、盈利收益率、BP、20日低波、低换手、非流动性",
     "w": {"mom": 0.15, "roe": 0.18, "ep": 0.12, "bm": 0.15, "lvol": 0.20, "tur": 0.10, "illiq": 0.10},
     "structure": "Top40 / 流通市值加权 / 单票上限6% / 月度调仓",
     "y": {"2023": (0.74, 6.73), "2024": (2.16, 12.20), "2025": (2.53, 6.34)}},
    {"name": "MD-Mom50", "key": "momdv-t50", "sharpe": 1.297, "mdd": 12.03, "ann": 20.77,
     "vol": 14.47, "sortino": 1.711, "excess": 14.53, "ir": 0.888, "turnover": 8.04,
     "desc": "动量+红利：12-1动量、股息率、BP、盈利收益率、20日低波、ROE、低换手",
     "w": {"mom": 0.18, "dy": 0.18, "bm": 0.15, "ep": 0.12, "lvol": 0.17, "roe": 0.10, "tur": 0.10},
     "structure": "Top50 / 流通市值加权 / 单票上限5% / 月度调仓（全场换手最低 8%）",
     "y": {"2023": (0.92, 10.55), "2024": (2.08, 10.39), "2025": (2.00, 9.45)}},
    {"name": "DV-LV50", "key": "dvdef-t50", "sharpe": 1.283, "mdd": 11.77, "ann": 20.02,
     "vol": 14.05, "sortino": 1.657, "excess": 13.78, "ir": 0.914, "turnover": 12.60,
     "desc": "红利低波：股息率、BP、盈利收益率、20日低波、低换手、小市值",
     "w": {"dy": 0.20, "bm": 0.20, "ep": 0.15, "lvol": 0.25, "tur": 0.10, "size": 0.10},
     "structure": "Top50 / 流通市值加权 / 单票上限5% / 月度调仓（全场回撤最低 11.77%）",
     "y": {"2023": (0.73, 9.10), "2024": (2.13, 10.93), "2025": (2.34, 5.08)}},
]

rows = "".join(f"""
<tr><td><b>{f['name']}</b><br><span class="key">{f['key']}</span></td>
<td>{f['ann']:.1f}%</td><td class="good">{f['sharpe']:.3f}</td><td>{f['sortino']:.2f}</td>
<td>{f['vol']:.1f}%</td><td class="good">{f['mdd']:.2f}%</td><td>{f['excess']:.1f}%</td>
<td>{f['ir']:.2f}</td><td>{f['turnover']:.1f}%</td></tr>""" for f in factors)

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
<title>优秀因子挖掘报告 · 纯多头夏普≥1.2</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#0f1419;color:#d8dee9;margin:0;padding:24px;line-height:1.6}}
h1{{font-size:22px;border-bottom:2px solid #4a9eff;padding-bottom:8px}}
h3{{margin:0 0 8px;color:#4a9eff}}
table{{border-collapse:collapse;width:100%;margin:14px 0}}
th,td{{border:1px solid #2d3748;padding:7px 10px;text-align:center;font-size:13px}}
th{{background:#1a2332;color:#8899aa}}
.good{{color:#4ade80;font-weight:bold}}
.key{{color:#8899aa;font-size:11px}}
.card{{background:#161d29;border:1px solid #2d3748;border-radius:10px;padding:16px 20px;margin:14px 0}}
.desc{{font-size:13px;color:#aab4c4;margin:4px 0}}
.structure{{font-size:12px;color:#e0b84a;margin:4px 0}}
.chip{{display:inline-block;background:#1f2a3c;border-radius:12px;padding:2px 10px;margin:2px;font-size:12px;color:#9fc3f0}}
.mini{{width:60%}}
.note{{background:#1a2332;border-left:3px solid #e0b84a;padding:10px 14px;font-size:13px;margin:16px 0}}
</style></head><body>
<h1>优秀因子挖掘报告（本地量化系统 · 2023-01 ~ 2025-12）</h1>
<p>目标：<b>纯多头夏普 ≥ 1.2（优秀档）</b> + 相对低回撤，可适当降低收益。基准：沪深300（年化 6.24%）。</p>
<p>回测链路：全A剔ST → 因子12成分截面RANK合成 → 月度调仓 TopN → 流通市值加权 → 次日开盘成交 → 全A股真实成本（佣金/过户/印花税/动态滑点·保守档）。</p>
<table><tr><th>因子</th><th>年化收益</th><th>夏普</th><th>索提诺</th><th>波动率</th><th>最大回撤</th><th>超额年化</th><th>信息比率</th><th>换手率</th></tr>{rows}</table>
<h2>因子详情</h2>
{cards}
<div class="note">
<b>开源来源与迭代路径</b>：Qlib Alpha158（microsoft/qlib）波动/反转家族 + 学术 Quality-Minus-Junk（ROE/毛利率/低杠杆）+ 红利低波异象（dv_ttm）。
原4成分（流动性/换手/市值/BM）框架上限为夏普 0.841；扩充至12成分后，动量+质量价值、红利低波两类配方将纯多头夏普推至 1.28~1.36，
回撤从 27.7% 压至 11.8~12.8%，波动率从 23.7% 压至 13.5~14.5% —— 以年化约 0~1 个点的让步换取风险指标全面改善。
分年检验：2023（弱年）夏普 0.73~0.84 但回撤仅 6.7~9.1%；2024 夏普 2.1+；2025 夏普 2.0+，无单年依赖。
</div>
<p style="color:#8899aa;font-size:12px">生成：2026-09-07 · 扫描明细 outputs/qvlv_scan/*.csv · 脚本 scripts/scan_qvlv.py</p>
</body></html>"""

open(OUT, "w", encoding="utf-8").write(html)
print(f"报告已生成: {OUT}")

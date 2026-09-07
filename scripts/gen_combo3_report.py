# -*- coding: utf-8 -*-
OUT = r"D:\量化交易\outputs\reports\三因子组合Combo3_2021-2025回测.html"

html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>三因子组合 Combo3 · 2021-2025 回测</title>
<style>
body{font-family:'Microsoft YaHei',sans-serif;background:#0f1419;color:#d8dee9;margin:0;padding:24px;line-height:1.6}
h1{font-size:22px;border-bottom:2px solid #4a9eff;padding-bottom:8px}
h2{font-size:17px;color:#4a9eff;margin-top:26px}
table{border-collapse:collapse;width:100%;margin:14px 0}
th,td{border:1px solid #2d3748;padding:7px 10px;text-align:center;font-size:13px}
th{background:#1a2332;color:#8899aa}
.good{color:#4ade80;font-weight:bold}.hl{background:#1f2a3c}
.chip{display:inline-block;background:#1f2a3c;border-radius:12px;padding:2px 10px;margin:2px;font-size:12px;color:#9fc3f0}
.note{background:#1a2332;border-left:3px solid #e0b84a;padding:10px 14px;font-size:13px;margin:16px 0}
</style></head><body>
<h1>三因子组合 Combo3 回测（2021-01 ~ 2025-12）</h1>
<p><b>成分</b>：MD-Mom50（动量+红利）· DV-LV50（红利低波）· GM-LV50（质量低波红利）<br>
<b>加权方法</b>：等权合成（机构最常用）——三因子截面 RANK 得分等权相加成复合因子，Top50 / 流通市值加权 / 单票上限5% / 月度调仓<br>
<b>口径</b>：全A剔ST · 次日开盘成交 · 全A股真实成本（佣金/过户/印花税/动态滑点·保守档）· 基准沪深300（区间年化 <b style="color:#e06c4a">-2.65%</b>）</p>

<h2>总绩效（2021-2025 五年）</h2>
<table><tr><th>方案</th><th>年化收益</th><th>夏普</th><th>索提诺</th><th>波动率</th><th>最大回撤</th><th>超额年化</th><th>信息比率</th></tr>
<tr><td>MD-Mom50 单因子</td><td>13.41%</td><td>0.808</td><td>1.060</td><td>14.12%</td><td>15.79%</td><td>16.06%</td><td>1.042</td></tr>
<tr><td>DV-LV50 单因子</td><td>14.81%</td><td>0.966</td><td>1.253</td><td>13.27%</td><td>11.78%</td><td>17.46%</td><td>1.164</td></tr>
<tr><td>GM-LV50 单因子</td><td>13.24%</td><td>0.852</td><td>1.158</td><td>13.20%</td><td>13.68%</td><td>15.89%</td><td>1.083</td></tr>
<tr class="hl"><td class="good">等权合成 Combo3（主方案）</td><td class="good">15.21%</td><td class="good">1.005</td><td class="good">1.361</td><td class="good">13.15%</td><td class="good">11.51%</td><td class="good">17.86%</td><td class="good">1.176</td></tr>
<tr><td>组合层面等权（对照）</td><td>13.89%</td><td>1.062</td><td>—</td><td>13.05%</td><td>12.40%</td><td>16.54%</td><td>—</td></tr>
</table>

<h2>等权合成 Combo3 分年表现</h2>
<table><tr><th>年份</th><th>收益</th><th>夏普</th><th>最大回撤</th><th>沪深300</th></tr>
<tr><td>2021</td><td>9.28%</td><td>0.972</td><td>7.11%</td><td class="good">-5.20%</td></tr>
<tr><td>2022</td><td>3.75%</td><td>0.336</td><td>10.20%</td><td class="good">-21.63%</td></tr>
<tr><td>2023</td><td>13.89%</td><td>1.143</td><td>10.40%</td><td class="good">-11.38%</td></tr>
<tr><td>2024</td><td>31.45%</td><td>1.509</td><td>11.51%</td><td>14.68%</td></tr>
<tr><td>2025</td><td>22.59%</td><td>1.818</td><td>8.04%</td><td>—</td></tr>
</table>
<div class="note"><b>五年全部正收益</b>，含 2022 大熊市（沪深300 -21.6%，Combo3 +3.75%）。
基准沪深300 五年年化 -2.65%，Combo3 年化 15.21%，超额 17.86%/年，最大回撤 11.51%（出现在 2024 年初微盘流动性冲击段）。</div>

<h2>三因子月收益相关性（2021-2025）</h2>
<table><tr><th></th><th>MD-Mom50</th><th>DV-LV50</th><th>GM-LV50</th></tr>
<tr><td><b>MD-Mom50</b></td><td>1.000</td><td>0.888</td><td>0.961</td></tr>
<tr><td><b>DV-LV50</b></td><td>0.888</td><td>1.000</td><td>0.943</td></tr>
<tr><td><b>GM-LV50</b></td><td>0.961</td><td>0.943</td><td>1.000</td></tr>
</table>
<div class="note"><b>合成效果</b>：三因子相关性高（0.89~0.96），但等权合成仍全面优于任意单因子——
夏普 1.005 &gt; 单因子最高 0.966，回撤 11.51% &lt; 单因子最低 11.78%，年化 15.21% &gt; 单因子最高 14.81%。
得分层面合成（选股时三票并一票）比收益层面混合更能利用重叠持仓的共识信号，且换手更低（8.2x vs 单因子 8.2~11.9x）。</div>
<p style="color:#8899aa;font-size:12px">生成：2026-09-07 · 脚本 scripts/backtest_combo3.py · 明细 outputs/final5/combo3_*.csv · 面板缓存 panel_cache_2125.pkl</p>
</body></html>"""

open(OUT, "w", encoding="utf-8").write(html)
print(f"报告已生成: {OUT}")

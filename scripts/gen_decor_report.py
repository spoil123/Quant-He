# -*- coding: utf-8 -*-
"""低相关因子挖掘结题报告"""
OUT = r"D:\量化交易\outputs\reports\低相关因子挖掘报告.html"

html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>低相关因子挖掘报告 · 两两相关&lt;0.5</title>
<style>
body{font-family:'Microsoft YaHei',sans-serif;background:#0f1419;color:#d8dee9;margin:0;padding:24px;line-height:1.6}
h1{font-size:22px;border-bottom:2px solid #4a9eff;padding-bottom:8px}
h2{font-size:17px;color:#4a9eff;margin-top:26px}
table{border-collapse:collapse;width:100%;margin:14px 0}
th,td{border:1px solid #2d3748;padding:7px 10px;text-align:center;font-size:13px}
th{background:#1a2332;color:#8899aa}
.good{color:#4ade80;font-weight:bold}.bad{color:#e06c4a}.mid{color:#e0b84a}
.note{background:#1a2332;border-left:3px solid #e0b84a;padding:10px 14px;font-size:13px;margin:16px 0}
.warn{background:#1a2332;border-left:3px solid #e06c4a;padding:10px 14px;font-size:13px;margin:16px 0}
</style></head><body>
<h1>低相关因子挖掘报告（目标：两两月收益相关 &lt; 0.5）</h1>
<p>回测口径与此前一致：全A剔ST · 月度调仓（另测双周）· 真实成本（保守档）· 2023-01 ~ 2025-12 · 基准沪深300。</p>

<div class="warn"><b>结论先行</b>：三轮扫描共测试 <b>32 个配方</b>后确认——在当前数据面板（日频行情+基础+财务）内，
<b>"5 个两两相关&lt;0.5 且夏普≥1.2"的因子组合不可达</b>。面板中只存在 <b>2 个相互独立的收益源</b>：
①质量+低波+红利+动量（夏普 1.28~1.44，互相相关 0.89~0.98）；②小市值+流动性（夏普 0.58~0.81，与前者的相关 0.39~0.40）。
其余所有信号（反转/盈利改善/低杠杆/低价格/波动收敛/低振幅/换手降温/动量加速度）要么本质是这两源的混合（相关 0.5~0.95），
要么在真实成本下无 alpha。</div>

<h2>各收益源实测结果（第三轮后汇总）</h2>
<table><tr><th>收益源</th><th>代表因子</th><th>夏普</th><th>最大回撤</th><th>与现有5因子最大相关</th><th>判定</th></tr>
<tr><td>质量+低波+红利+动量</td><td>QV2-Mom60 / QV-Mom40 等5个</td><td class="good">1.28~1.44</td><td>11.6~12.7%</td><td>0.89~0.98（互相关）</td><td class="good">强但同源</td></tr>
<tr><td>小市值+流动性</td><td>SCap-100（Top100/上限4%）</td><td class="mid">0.812</td><td class="bad">36.9%</td><td class="good">0.397</td><td class="mid">独立但弱、回撤大</td></tr>
<tr><td>小市值+盈利/红利/低波混合</td><td>SCapQ-100 / SCapDV-50 等</td><td>0.26~0.61</td><td>33~43%</td><td>0.40~0.55</td><td class="bad">混合后两面不讨好</td></tr>
<tr><td>短期反转 rev20（月度/双周）</td><td>Rev-50 / Rev-BW</td><td class="bad">-0.26~0.01</td><td>38~50%</td><td>0.52~0.59</td><td class="bad">成本后无 alpha</td></tr>
<tr><td>盈利改善 roechg（ROE季度变化）</td><td>RoeChg-50</td><td class="bad">0.05</td><td>29%</td><td>0.58</td><td class="bad">无 alpha</td></tr>
<tr><td>低杠杆 / 低价格 / 动量加速度</td><td>LevQ-50 / LP-50 / MomAcc-50</td><td class="bad">-0.45~0.28</td><td>31~44%</td><td>0.54~0.69</td><td class="bad">无 alpha 或高相关</td></tr>
<tr><td>波动收敛 / 低振幅 / 换手降温</td><td>VolChg-50 / Amp-50 / TurnCool-50</td><td>0.14~0.33</td><td>29~43%</td><td>0.91~0.95（vs SCap）</td><td class="bad">本质是低波/流动性换皮</td></tr>
</table>

<h2>唯一达标的低相关组合：QV2-Mom60 × SCap-100（相关 0.397）</h2>
<table><tr><th>QV2-Mom60 占比</th><th>SCap-100 占比</th><th>年化</th><th>夏普*</th><th>最大回撤</th><th>波动</th></tr>
<tr><td>100%</td><td>0%</td><td>21.45%</td><td>1.507</td><td>12.47%</td><td>13.51%</td></tr>
<tr><td class="good"><b>90%</b></td><td class="good"><b>10%</b></td><td class="good">21.85%</td><td class="good"><b>1.553</b></td><td>12.50%</td><td class="good">13.29%</td></tr>
<tr><td>80%</td><td>20%</td><td>22.17%</td><td>1.553</td><td>12.54%</td><td>13.49%</td></tr>
<tr><td>70%</td><td>30%</td><td>22.43%</td><td>1.508</td><td>12.57%</td><td>14.08%</td></tr>
<tr><td>60%</td><td>40%</td><td>22.61%</td><td>1.433</td><td>14.08%</td><td>15.02%</td></tr>
<tr><td>50%</td><td>50%</td><td>22.74%</td><td>1.342</td><td class="bad">18.17%</td><td>16.26%</td></tr>
</table>
<p style="font-size:12px;color:#8899aa">*此表夏普为 rf=0 简化口径（各行同口径可比，与含无风险利率的正式口径 1.44 略有差异）。</p>
<div class="note"><b>推荐配置：QV2-Mom60 90% + SCap-100 10%（或 80/20）</b>。
加入 10~20% 小市值因子后：年化 +0.4~0.7 个点、夏普 1.51→1.55、波动略降、回撤几乎不变——这是相关 0.397 的两个独立源给出的真实分散增益。
SCap 占比超过 30% 后，小市值因子 36.9% 的固有回撤开始主导组合（2024-01 微盘股流动性危机所致），风险不降反升。</div>

<h2>为什么 5 个低相关高夏普因子在当前面板不可达</h2>
<div class="note">
① <b>分组诊断</b>：5分组各组年化显示因子收益非单调——最低分组年化仅 3.7~8.7%（显著差），但最高分组不比中间组好，
alpha 本质是"排除烂股票"型，天然集中在质量/低波/红利一个方向上；<br>
② <b>反转与盈利改善</b>在 A 股日频+真实成本框架下无稳定 alpha（双周调仓更差，信号衰减快于持有期且成本占比高）；<br>
③ <b>其余量价信号</b>（振幅/波动收敛/换手）与低波因子信息重叠 0.9+，不是独立源。
</div>
<div class="warn"><b>要真正凑满 5 个两两&lt;0.5 的高夏普因子，需要新信息维度</b>（当前面板没有的）：
分钟级行情（隔夜跳空/日内动量）、行业/概念分类（行业轮动与行业中性）、分析师一致预期（盈余修正）、
龙虎榜/北向资金（资金流）、可转债/衍生品隐含信息。建议下一步先扩数据层再挖因子。</div>

<p style="color:#8899aa;font-size:12px">生成：2026-09-07 · 扫描脚本 scripts/scan_decor.py / scan_decor2.py / scan_decor3.py / blend_scan.py · 明细 outputs/final5/decor*.csv · blend_curve.csv</p>
</body></html>"""

open(OUT, "w", encoding="utf-8").write(html)
print(f"报告已生成: {OUT}")

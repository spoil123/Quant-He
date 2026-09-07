# -*- coding: utf-8 -*-
"""本地系统 PandaAI Top5 因子复现结果 → HTML 报告"""
import json, html, subprocess, sys

data_rows = [
    # 因子, 周期, IC(5d), ICIR(5d), IC(20d), ICIR(20d), IC>0(20d), t值, 单调性, Q5年化%(20d口径), 多空月均%(20d)
    ("b6-mix4", 10, 0.1119, 0.6763, 0.1501, 0.8598, 0.778, 22.86, 1.0, 28.1, 2.354),
    ("hp1-c1", 1, 0.1105, 0.6596, 0.1497, 0.8531, 0.777, 22.68, 1.0, 28.3, 2.364),
    ("b7-f_mix4_v3", 10, 0.1085, 0.6459, 0.1473, 0.8405, 0.772, 22.35, 1.0, 27.7, 2.289),
    ("b13-f_c3_5th_a", 3, 0.1031, 0.6128, 0.1429, 0.8237, 0.777, 21.90, 1.0, 28.4, 2.304),
    ("lowdd_r1k_c4", 4, 0.1000, 0.5968, 0.1361, 0.7877, 0.767, 20.94, 1.0, 26.2, 2.069),
]

# 平台(多头组)对照
platform = {
    "b6-mix4": ("0.0667", "0.4126", "30.26%", "1.175", "23.56%"),
    "b7-f_mix4_v3": ("0.0651", "0.3979", "30.03%", "1.168", "23.18%"),
    "b13-f_c3_5th_a": ("0.0429", "0.3010", "34.14%", "1.161", "37.59%"),
    "lowdd_r1k_c4": ("0.0421", "0.2827", "30.25%", "1.167", "23.23%"),
    "hp1-c1": ("0.0269", "0.1869", "33.44%", "1.180", "40.75%"),
}

meta = {
    "b6-mix4": "流动性0.30+低换手0.30+小市值0.20+BM0.20",
    "hp1-c1": "流动性0.30+低换手0.25+小市值0.20+BM0.25（LOGABS口径）",
    "b7-f_mix4_v3": "流动性0.25+低换手0.25+小市值0.20+BM0.30",
    "b13-f_c3_5th_a": "流动性0.20+低换手0.20+小市值0.25+BM0.35",
    "lowdd_r1k_c4": "流动性0.2125+低换手0.2125+小市值0.125+BM0.45",
}

rows = ""
for r in data_rows:
    name = r[0]
    pi = platform[name]
    rows += f"""<tr>
      <td><b>{name}</b><br><span class="mono">周期{r[1]}日</span></td>
      <td class="mono sm">{meta[name]}</td>
      <td class="strong">{r[4]:.4f}</td>
      <td>{r[5]:.4f}</td>
      <td>{r[6]:.1%}</td>
      <td>{r[7]:.1f}</td>
      <td>{r[8]:.1f}</td>
      <td>{r[9]:.1f}%</td>
      <td>{r[10]:.2f}%</td>
      <td class="sub">{pi[0]} / {pi[1]}</td>
      <td class="sub">{pi[2]}</td>
    </tr>"""

html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PandaAI Top5 因子 · 本地系统复现评估报告</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--sub:#8b949e;--gold:#f0b90b;--green:#7ee787;--blue:#4e8cff}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.65;padding:36px 18px}}
.wrap{{max-width:1100px;margin:0 auto}}
h1{{font-size:25px;margin-bottom:6px}}
h2{{font-size:19px;margin:28px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
h3{{font-size:15px;margin:16px 0 8px;color:var(--gold)}}
.sub{{color:var(--sub);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0}}
th{{background:#21262d;color:var(--sub);text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #21262d}}
.strong{{color:var(--gold);font-weight:700}}
.green{{color:var(--green)}}
.sub{{color:var(--sub);font-size:11px}}
.mono{{font-family:Consolas,monospace}}
.sm{{font-size:11px}}
.tip{{background:rgba(78,140,255,.08);border-left:3px solid var(--blue);padding:10px 14px;border-radius:0 6px 6px 0;font-size:13px;margin:10px 0}}
.warn{{background:rgba(248,81,73,.08);border-left:3px solid #f85149;padding:10px 14px;border-radius:0 6px 6px 0;font-size:13px;margin:10px 0}}
ul,ol{{margin:6px 0 6px 20px;font-size:13px}}
li{{margin:4px 0}}
.bar{{height:10px;border-radius:5px;background:#21262d;overflow:hidden;margin:3px 0}}
.bar i{{display:block;height:100%;border-radius:5px;background:var(--green)}}
.footer{{color:var(--sub);font-size:12px;margin-top:26px;text-align:center}}
</style></head><body><div class="wrap">

<h1>PandaAI Top5 因子 · 本地系统复现评估报告</h1>
<div class="sub">本地量化系统（D:\量化交易）· 窗口 2023-01-01 ~ 2025-12-31 · 全A剔ST · 沪深 5352 只 / 1083 交易日</div>

<h2>一、核心结果（本地数据实测）</h2>
<div class="card" style="overflow-x:auto">
<table><thead><tr>
<th>因子</th><th>成分权重</th><th>IC(20日)</th><th>ICIR(20日)</th><th>IC&gt;0占比</th><th>t值</th><th>单调性</th><th>Q5年化</th><th>多空月均</th><th>平台IC/ICIR对照</th><th>平台年化</th>
</tr></thead><tbody>{rows}</tbody></table>
</div>

<div class="card">
<h3>结论</h3>
<ul>
<li><b>5 个因子在本地系统全部显著有效</b>：20 日 IC 0.136~0.150、ICIR 0.79~0.86、IC&gt;0 占比 77%+、t 值 21~23、单调性全部 1.0（Q1→Q5 严格递增）。</li>
<li><b>多头组（Q5）年化 26~28%</b>（20 日持有、月度换仓口径），多空组合月均 2.1~2.4%。</li>
<li>排名：<b>b6-mix4 ≈ hp1-c1</b> 并列第一（IC 0.150/0.150），lowdd_r1k_c4 略弱（IC 0.136）。</li>
</ul>
</div>

<h2>二、本地 vs 平台 差异说明（重要）</h2>
<div class="card">
<div class="warn"><b>数值不可直接对比</b>：本地 IC 用「未来 20 日收益」截面秩相关（0.14~0.15），平台 IC 为「未来 1 日」口径（0.03~0.07）。预测期越长 IC 天然越高，两者都是各自体系的合格标准。</div>
<ul>
<li><b>同向验证通过</b>：本地排名（b6-mix4/hp1-c1 &gt; b7 &gt; b13 &gt; lowdd）与平台预测力排名（b6-mix4 &gt; b7 &gt; b13 &gt; lowdd &gt; hp1）基本一致 —— 说明因子结构在真实本地数据上可复现。</li>
<li><b>hp1-c1 例外</b>：平台多头组 IC 最低（0.027）但本地 20 日 IC 第二（0.150）。原因是平台 hp1-c1 为 1 日调仓，IC 按 1 日衰减快；本地按 20 日持有评估，弱化了日频噪音。</li>
<li><b>数据差异</b>：本地 BM 用 bps/close（ann_date PIT 对齐），平台用 book_to_market_ratio_ttm；市值本地用流通市值 float_mv，平台为总市值口径。方向一致。</li>
</ul>
</div>

<h2>三、各因子对比条形图（20日IC / ICIR / Q5年化）</h2>
<div class="card">
<h3>20 日 IC（满分 0.16）</h3>
<div class="bar"><i style="width:{0.1501/0.16*100:.0f}%"></i></div><span class="sub">b6-mix4 0.1501</span>
<div class="bar"><i style="width:{0.1497/0.16*100:.0f}%"></i></div><span class="sub">hp1-c1 0.1497</span>
<div class="bar"><i style="width:{0.1473/0.16*100:.0f}%"></i></div><span class="sub">b7-f_mix4_v3 0.1473</span>
<div class="bar"><i style="width:{0.1429/0.16*100:.0f}%"></i></div><span class="sub">b13-f_c3_5th_a 0.1429</span>
<div class="bar"><i style="width:{0.1361/0.16*100:.0f}%"></i></div><span class="sub">lowdd_r1k_c4 0.1361</span>
<h3>ICIR（满分 0.9）</h3>
<div class="bar"><i style="width:{0.8598/0.9*100:.0f}%"></i></div><span class="sub">b6-mix4 0.8598</span>
<div class="bar"><i style="width:{0.8531/0.9*100:.0f}%"></i></div><span class="sub">hp1-c1 0.8531</span>
<div class="bar"><i style="width:{0.8405/0.9*100:.0f}%"></i></div><span class="sub">b7-f_mix4_v3 0.8405</span>
<div class="bar"><i style="width:{0.8237/0.9*100:.0f}%"></i></div><span class="sub">b13-f_c3_5th_a 0.8237</span>
<div class="bar"><i style="width:{0.7877/0.9*100:.0f}%"></i></div><span class="sub">lowdd_r1k_c4 0.7877</span>
</div>

<div class="footer">本地复现脚本：D:\量化交易\scripts\replay_pandaai_top5.py · 原始CSV：outputs\factors\pandaai_top5_local_2023-2025_*.csv · 生成 2026-09-06</div>
</div></body></html>"""

out = r"D:/量化交易/outputs/reports/PandaAI_Top5_本地复现评估.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(html_doc)
print(f"报告已生成: {out}, 大小 {len(html_doc.encode('utf-8'))}")

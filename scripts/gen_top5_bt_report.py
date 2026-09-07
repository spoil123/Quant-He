# -*- coding: utf-8 -*-
"""本地系统 PandaAI Top5 组合回测 → 完整绩效报告 HTML（含净值曲线）"""
import json

# 从 CSV 读回测指标
import pandas as pd
import glob, os

bt_files = sorted(glob.glob(r"D:/量化交易/outputs/top5_bt/pandaai_top5_bt_2023-2025_*.csv"))
nav_files = sorted(glob.glob(r"D:/量化交易/outputs/top5_bt/pandaai_top5_nav_2023-2025_*.json"))
bt_csv = bt_files[-1]
nav_json = nav_files[-1]

df = pd.read_csv(bt_csv, encoding="utf-8-sig")
nav = json.load(open(nav_json, encoding="utf-8"))

meta = {
    "b6-mix4": "流动性0.30+低换手0.30+小市值0.20+BM0.20",
    "b7-f_mix4_v3": "流动性0.25+低换手0.25+小市值0.20+BM0.30",
    "b13-f_c3_5th_a": "流动性0.20+低换手0.20+小市值0.25+BM0.35",
    "lowdd_r1k_c4": "流动性0.21+低换手0.21+小市值0.13+BM0.45",
    "hp1-c1": "流动性0.30+低换手0.25+小市值0.20+BM0.25",
}
ORDER = ["b6-mix4", "b7-f_mix4_v3", "b13-f_c3_5th_a", "lowdd_r1k_c4", "hp1-c1"]

def pct(x):
    try:
        v = float(x)
        return f"{v*100:.2f}%" if abs(v) <= 3 else f"{v:.2f}"
    except (TypeError, ValueError):
        return str(x)

def svg_nav(series_dict, colors, w=900, h=260):
    """多条净值曲线叠加 SVG。"""
    if not series_dict:
        return ""
    # 统一日期网格
    all_dates = sorted({d for s in series_dict.values() for d in s["date"]})
    if not all_dates:
        return ""
    lo, hi = 1.0, 1.0
    grids = {}
    for name, s in series_dict.items():
        m = {d: float(v) for d, v in zip(s["date"], s["nav"])}
        grids[name] = m
        vals = [v for v in m.values() if v == v]
        if vals:
            lo = min(lo, min(vals)); hi = max(hi, max(vals))
    rng = (hi - lo) or 1.0
    pad = (hi - lo) * 0.08 + 1e-9
    lo -= pad; hi += pad
    n = len(all_dates)
    W, H = w, h
    ML, MR, MT, MB = 50, 16, 14, 30
    def X(i): return ML + i * (W - ML - MR) / max(n - 1, 1)
    def Y(v): return MT + (1 - (v - lo) / (hi - lo)) * (H - MT - MB)
    # y 轴刻度
    yt = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        yt.append(f'<text x="{ML-8}" y="{Y(v)+4}" text-anchor="end" font-size="10" fill="#8b949e">{v:.2f}</text>')
    parts = []
    for name, m in grids.items():
        pts = []
        for i, d in enumerate(all_dates):
            v = m.get(d)
            if v is None or v != v:
                continue
            pts.append(f"{X(i):.1f},{Y(v):.1f}")
        if len(pts) >= 2:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{colors.get(name, "#f0b90b")}" stroke-width="1.6"/>')
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;background:#0d1117;border-radius:8px">'
            + "".join(yt)
            + "".join(parts) + "</svg>")

def legend(items):
    return '<div style="display:flex;flex-wrap:wrap;gap:14px;margin:10px 0;font-size:12px">' + "".join(
        f'<span><span style="display:inline-block;width:12px;height:3px;background:{c};vertical-align:middle;margin-right:6px"></span>{n}</span>'
        for n, c in items) + "</div>"

# 指标表
rows = ""
for _, r in df.iterrows():
    name = r["因子"]
    rows += f"""<tr>
      <td><b>{name}</b><br><span class="mono">{meta.get(name, '')}</span></td>
      <td>{int(r['调仓周期(日)'])}</td>
      <td class="strong">{pct(r['总收益率'])}</td>
      <td class="strong">{pct(r['年化收益率'])}</td>
      <td>{pct(r['年化波动率'])}</td>
      <td class="strong">{pct(r['夏普比率'])}</td>
      <td>{pct(r['索提诺比率'])}</td>
      <td>{pct(r['最大回撤'])}</td>
      <td>{pct(r.get('基准年化收益率'))}</td>
      <td class="strong">{pct(r['年化Alpha'])}</td>
      <td>{pct(r['Beta'])}</td>
      <td class="strong">{pct(r['超额年化收益率'])}</td>
      <td>{pct(r['信息比率'])}</td>
      <td>{pct(r['年化换手率'])}</td>
    </tr>"""

colors = {"b6-mix4": "#f0b90b", "b7-f_mix4_v3": "#4e8cff",
          "b13-f_c3_5th_a": "#7ee787", "lowdd_r1k_c4": "#f78166",
          "hp1-c1": "#bc8cff", "沪深300": "#555a66"}

# 净值图（只画与基准, Top 5 同图太密，分两组：单因子+基准）
def svg_group(names):
    d = {n: nav[n] for n in names if n in nav}
    return svg_nav(d, colors)

svg1 = svg_group(["b6-mix4", "b7-f_mix4_v3", "沪深300"])
svg2 = svg_group(["b13-f_c3_5th_a", "lowdd_r1k_c4", "hp1-c1", "沪深300"])
leg1 = legend([("b6-mix4", colors["b6-mix4"]), ("b7-f_mix4_v3", colors["b7-f_mix4_v3"]), ("沪深300", colors["沪深300"])])
leg2 = legend([("b13-f_c3_5th_a", colors["b13-f_c3_5th_a"]), ("lowdd_r1k_c4", colors["lowdd_r1k_c4"]), ("hp1-c1", colors["hp1-c1"]), ("沪深300", colors["沪深300"])])

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PandaAI Top5 因子 · 本地组合回测绩效报告</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--sub:#8b949e;--gold:#f0b90b;--green:#7ee787;--red:#f78166;--blue:#4e8cff}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.65;padding:36px 18px}}
.wrap{{max-width:1200px;margin:0 auto}}
h1{{font-size:25px;margin-bottom:6px}}
h2{{font-size:19px;margin:28px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
h3{{font-size:15px;margin:16px 0 8px;color:var(--gold)}}
.sub{{color:var(--sub);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin:8px 0}}
th{{background:#21262d;color:var(--sub);text-align:left;padding:8px 8px;border-bottom:1px solid var(--border);white-space:nowrap}}
td{{padding:6px 8px;border-bottom:1px solid #21262d;white-space:nowrap}}
.strong{{color:var(--gold);font-weight:700}}
.mono{{font-family:Consolas,monospace;font-size:11px;color:var(--sub)}}
.tip{{background:rgba(78,140,255,.08);border-left:3px solid var(--blue);padding:10px 14px;border-radius:0 6px 6px 0;font-size:13px;margin:10px 0}}
.warn{{background:rgba(248,81,73,.08);border-left:3px solid #f85149;padding:10px 14px;border-radius:0 6px 6px 0;font-size:13px;margin:10px 0}}
.good{{color:var(--green);font-weight:600}}
ul,ol{{margin:6px 0 6px 20px;font-size:13px}}
li{{margin:4px 0}}
.footer{{color:var(--sub);font-size:12px;margin-top:24px;text-align:center}}
</style></head><body><div class="wrap">

<h1>PandaAI Top5 因子 · 本地系统完整组合回测绩效</h1>
<div class="sub">本地量化系统（D:\量化交易）· 2023-01-01 ~ 2025-12-31 · Top50 月度调仓 · 流通市值加权 · 次日开盘成交 · 全A股成本(保守档:滑点×1.5)</div>

<h2>一、绩效总表（含常用指标全集）</h2>
<div class="card" style="overflow-x:auto">
<table><thead><tr>
<th>因子</th><th>周期</th><th>累计收益</th><th>年化收益</th><th>年化波动</th><th>夏普</th><th>索提诺</th><th>最大回撤</th><th>基准年化</th><th>年化Alpha</th><th>Beta</th><th>超额年化</th><th>信息比率</th><th>年化换手</th>
</tr></thead><tbody>{rows}</tbody></table>
<div class="tip">基准 = 沪深300（同期年化 +6.24%）。所有指标均含完整交易成本（佣金/过户费/印花税/动态滑点，保守档）。</div>
</div>

<h2>二、净值曲线（因子 vs 沪深300）</h2>
<div class="card">
<h3>高流动性/高价值组</h3>
{leg1}
{svg1}
<h3>年化/均衡/BM重仓组</h3>
{leg2}
{svg2}
</div>

<h2>三、解读</h2>
<div class="card">
<ul>
<li><b>全部跑赢基准</b>：5 因子年化 18.0%~20.9%，vs 沪深300 同期年化 +6.2%；超额年化 11.8%~14.6%，Beta 0.84-0.87。</li>
<li><b>b13-f_c3_5th_a 综合最优</b>：累计 +72.8%、年化 20.9%、夏普 0.726、Alpha 15.9%、信息比率 0.64 —— 收益与稳健兼顾。</li>
<li><b>b7-f_mix4_v3 次之</b>：累计 +69.3%、年化 20.0%、夏普 0.723。</li>
<li><b>lowdd_r1k_c4 回撤控制最好</b>：最大回撤 27.4%（全场最低），夏普 0.698，代价是收益略低（年化 18.5%）。</li>
<li><b>b6-mix4 相对最弱</b>：年化 18.0%/夏普 0.637（IC 虽最高，但小市值+高流动性权重导致月度换手偏高的成本拖累）。</li>
<li><b>对比平台</b>：平台多空组合夏普 1.5-2.4 显著高于本地单边多头（0.64-0.73）—— 多空策略对冲掉市场 beta，夏普天然更高；本地为纯多头 Top50，含市场波动，两者口径不同。</li>
</ul>
<div class="warn"><b>诚实提示</b>：本窗口（2023-2025）A 股小市值/微盘风格占优，Top50 小盘多头天然受益。单因子月度多头夏普 0.6-0.7 在 A 股属合格水平；若要更高夏普需多因子合成（本地 Top50 五因子合成样本外夏普 ~0.6-0.7 量级同）。任何样本外结论以 walk-forward 为准。</div>
</div>

<div class="footer">回测脚本 scripts/backtest_pandaai_top5.py · 明细 CSV / 净值 JSON 在 outputs/top5_bt/ · 生成 2026-09-06</div>
</div></body></html>"""

out = r"D:/量化交易/outputs/reports/PandaAI_Top5_组合回测绩效.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print(f"报告已生成: {out}, 大小 {len(html.encode('utf-8'))}")

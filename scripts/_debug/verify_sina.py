# -*- coding: utf-8 -*-
import akshare as ak, pandas as pd, sys
pd.set_option("display.width", 200)

print("=== 1. 新浪日线：前复权 vs 后复权 vs 不复权 ===")
qfq = ak.stock_zh_a_daily(symbol="sh600000", adjust="qfq")
hfq = ak.stock_zh_a_daily(symbol="sh600000", adjust="hfq")
raw = ak.stock_zh_a_daily(symbol="sh600000", adjust="")
for nm, df in [("qfq", qfq), ("hfq", hfq), ("none", raw)]:
    d = df.tail(3)[["date","open","close","volume","outstanding_share","turnover"]]
    print(f"--- {nm} (共{len(df)}行) ---"); print(d.to_string(index=False))

print("\n=== 2. 流通股本是否随历史变化（关键：算流通市值用） ===")
s = raw.set_index("date")["outstanding_share"]
for yr in ["2000","2005","2010","2015","2020","2023"]:
    sub = s[s.index.astype(str).str.startswith(yr)]
    if len(sub): print(f"  {yr}年末流通股本: {sub.iloc[-1]:,.0f} 股 ({sub.iloc[-1]/1e8:.2f} 亿股)")
print(f"  唯一值个数: {s.nunique()} / 总行数 {len(s)}  -> {'历史序列✓' if s.nunique()>50 else '恒定值✗(仅当前值)'}")

print("\n=== 3. 换手率 turnover 单位检查 ===")
print(raw.tail(3)[["date","volume","outstanding_share","turnover"]].to_string(index=False))
calc = raw["volume"]/raw["outstanding_share"]*100
print(f"  计算值 volume/outstanding_share*100 尾部: {calc.tail(3).round(4).tolist()}")
print(f"  接口 turnover 尾部: {raw['turnover'].tail(3).round(4).tolist()}")

print("\n=== 4. 新浪指数接口 ===")
try:
    idx = ak.stock_zh_index_daily(symbol="sh000300")
    print(f"  [OK] 共{len(idx)}行, 字段={list(idx.columns)}")
    print(idx.tail(3).to_string(index=False))
except Exception as e:
    print(f"  [FAIL] {type(e).__name__}: {str(e)[:80]}"); sys.stdout.flush()

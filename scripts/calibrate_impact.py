# -*- coding: utf-8 -*-
"""
impact_coef 实证校准入口（第 5 层执行层）。

从成交记录（execution_trade）回归真实 impact_coef，替代 costs.yaml 的经验值。
用法：
    # 看当前样本概况（含样本来源判定）
    python scripts/calibrate_impact.py

    # 回归并打印结论（不写库、不回写）
    python scripts/calibrate_impact.py --regress

    # 回归并留档 impact_calib 表（同区间同方法 upsert，不产生重复行）
    python scripts/calibrate_impact.py --regress --save

    # 回归并把结论回写 costs.yaml（真实成交样本才允许；模拟样本须 --force）
    python scripts/calibrate_impact.py --regress --write-back

    # 只看看会改成什么样，不动文件
    python scripts/calibrate_impact.py --regress --write-back --dry-run

    # 清理 impact_calib 里内容完全重复的冗余行（保留 id 最小的一条）
    python scripts/calibrate_impact.py --prune

    # 指定区间
    python scripts/calibrate_impact.py --regress --start 2026-09-01 --end 2026-09-30

流程：
    成交回报(TradeStore) -> 关联当日成交额 -> 算 participation/slip_bps
    -> 回归 slip_bps ~ base + impact*sqrt(participation)*100 -> 输出/留档/回写

--------------------------------------------------------------------------
样本来源门控（本脚本的核心约束，别删）
--------------------------------------------------------------------------
broker='paper' 的成交是 scripts/paper_trade_sim.py 造的模拟撮合样本：成交价
由「人为设定的真值」生成。回归这类样本得到的系数，只是对已知真值的估计误差，
不含任何真实市场冲击信息。把它回写进配置等于自我循环 —— 配置里躺着一个看
起来更精确（如 0.968）实则与 1.0 无差别的数，还会贴着"实证校准"的假标签，
让后面的人误以为这个数已经被证明过。

因此 --write-back 默认只接受真实券商回报样本；模拟样本必须显式 --force，
且回写内容会强制标注"模拟样本，非实证值"。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import get_config
from src.common.db import execute, read_sql
from src.common.logger import logger
from src.layer5_execution.impact import (
    compute_slip_bps,
    load_trades_with_amount,
    regress_impact,
    sample_source,
    summarize,
)
from src.layer5_execution.trade_store import TradeStore

COSTS_YAML = ROOT / "config" / "costs.yaml"

# costs.yaml 中由本脚本自动维护的标记块（回写时只替换块内内容，不动其他注释）
_BEGIN = "# ========== BEGIN AUTO-CALIB"
_END = "# ========== END AUTO-CALIB =========="

_SRC_LABEL = {
    "real": "真实券商成交回报",
    "simulated": "模拟撮合(broker=paper)",
    "mixed": "真实+模拟混合",
    "unknown": "未标注来源",
}

_FORCE_WARN = ("样本为模拟撮合(broker=paper)：该系数是对造数真值的回归估计，"
               "不是实证冲击系数，不可当作实盘成本依据")


# ================================================================ costs.yaml 回写

def _replace_scalar(text: str, key: str, value: float) -> Tuple[str, int]:
    """替换 `  key: <数值>   # 注释` 里的数值，保留行尾注释与缩进。"""
    pat = re.compile(
        rf"^(\s{{2,}}{re.escape(key)}:\s*)([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(\s*#.*)?$",
        re.M,
    )
    return pat.subn(lambda m: f"{m.group(1)}{value}{m.group(3) or ''}", text, count=1)


def _replace_block(text: str, lines: List[str]) -> str:
    """替换 AUTO-CALIB 标记块；标记缺失时返回空串（拒绝回写，避免破坏注释）。

    起始位置回退到 BEGIN 所在行的行首：缩进完全由 lines 自己控制，
    否则每次回写都会把原文件的缩进再叠加一遍（缩进漂移）。
    """
    i, j = text.find(_BEGIN), text.find(_END)
    if i < 0 or j < 0:
        logger.error("costs.yaml 缺少 AUTO-CALIB 标记块 —— 拒绝回写，"
                     "请先恢复 BEGIN/END AUTO-CALIB 标记")
        return ""
    line_start = text.rfind("\n", 0, i) + 1     # BEGIN 所在行的行首
    after_end = j + len(_END)                   # END 标记结束处（其后内容保留）
    return text[:line_start] + "\n".join(lines) + text[after_end:]


def _calib_block(result: Dict, src: str, dist: Dict[str, int], period: str,
                 prev_coef, prev_base) -> List[str]:
    """生成 AUTO-CALIB 块内容。"""
    now = datetime.now()
    lines = [
        f"  {_BEGIN}（本块由 calibrate_impact.py --write-back 自动重写，勿手改）==========",
        f"  # 回写时间 {now:%Y-%m-%d %H:%M} —— "
        f"回归 slip_bps = base_bps + impact_coef*sqrt(participation)*100",
        f"  #   样本 n={result['n']}  区间 {period}  方法 {result['method']}"
        f"  R²={result['r_squared']:.4f}",
        f"  #   impact_coef: {prev_coef} -> {result['impact_coef']:.6f}",
        f"  #   base_bps:    {prev_base} -> {result['base_bps']:.6f}",
        f"  #   样本来源: {_SRC_LABEL.get(src, src)}  分布 {dist}",
    ]
    if src == "real":
        lines.append(
            "  #   该系数为真实成交回归结果，可作为实证成本依据；"
            "滑点假设不确定性已下降，"
        )
        lines.append(
            "  #   可酌情下调 tiers.conservative.slip_multiplier（原 1.5 是为覆盖"
            "未实证风险）"
        )
    else:
        lines.append(f"  #   ⚠ {_FORCE_WARN}")
    lines.append(f"  {_END}")
    return lines


def write_back_costs(result: Dict, src: str, dist: Dict[str, int],
                     period: str, dry_run: bool = False) -> bool:
    """把回归结论写回 costs.yaml 的 slippage 段。

    会先备份原文件（costs.yaml.bak.YYYYmmdd_HHMMSS），再改写：
      1) slippage.impact_coef 数值
      2) slippage.base_bps 数值
      3) AUTO-CALIB 说明块（记录样本量 / R² / 来源 / 旧值）
    失败不留下半改写的残file：先整体拼好再一次性写入。
    """
    if result.get("impact_coef") is None:
        logger.error("回归结论无效，拒绝回写")
        return False

    costs = get_config("costs") or {}
    slip = costs.get("slippage", {}) or {}
    prev_coef, prev_base = slip.get("impact_coef"), slip.get("base_bps")

    text = COSTS_YAML.read_text(encoding="utf-8")
    new_text = _replace_block(text, _calib_block(result, src, dist, period,
                                                 prev_coef, prev_base))
    if not new_text:
        return False
    new_text, n1 = _replace_scalar(new_text, "impact_coef",
                                   round(float(result["impact_coef"]), 6))
    new_text, n2 = _replace_scalar(new_text, "base_bps",
                                   round(float(result["base_bps"]), 6))
    if n1 == 0 or n2 == 0:
        logger.error(f"costs.yaml 数值替换失败（impact_coef={n1} base_bps={n2}），"
                     "拒绝写入 —— 请检查 slippage 段结构")
        return False

    if dry_run:
        print("\n[--dry-run] 将写入以下内容（文件未改动）：")
        print("-" * 70)
        print("\n".join(_calib_block(result, src, dist, period, prev_coef, prev_base)))
        print(f"  impact_coef: {prev_coef} -> {round(float(result['impact_coef']), 6)}")
        print(f"  base_bps:    {prev_base} -> {round(float(result['base_bps']), 6)}")
        print("-" * 70)
        return True

    bak = COSTS_YAML.with_suffix(
        f".yaml.bak.{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(COSTS_YAML, bak)
    COSTS_YAML.write_text(new_text, encoding="utf-8")
    logger.info(f"已回写 costs.yaml（备份 {bak.name}）: "
                f"impact_coef {prev_coef} -> {result['impact_coef']:.6f}, "
                f"base_bps {prev_base} -> {result['base_bps']:.6f}")
    return True


# ================================================================ impact_calib 留档

def _find_existing(ps, pe, method) -> Optional[int]:
    """查找同区间同方法的既有留档 id（用于 upsert，避免重复行）。"""
    where = "period_start IS NULL" if ps is None else "period_start = :ps"
    if pe is None:
        where += " AND period_end IS NULL"
    else:
        where += " AND period_end = :pe"
    params = {"ps": ps, "pe": pe, "m": method}
    df = read_sql(
        f"SELECT id FROM impact_calib WHERE {where} AND method = :m ORDER BY id LIMIT 1",
        params,
    )
    return int(df["id"].iloc[0]) if not df.empty else None


def upsert_calib(result: Dict, ps, pe, note: str) -> None:
    """同 (区间, 方法) 存在则更新，否则插入 —— 根治重复留档。"""
    rid = _find_existing(ps, pe, result["method"])
    if rid is not None:
        execute(
            """UPDATE impact_calib
                  SET calib_date = :d, n_trades = :nt, base_bps = :bb,
                      impact_coef = :ic, r_squared = :r2, note = :note
                WHERE id = :id""",
            {"d": datetime.now().date(), "nt": int(result["n"]),
             "bb": result["base_bps"], "ic": result["impact_coef"],
             "r2": result["r_squared"], "note": note, "id": rid},
        )
        logger.info(f"回归结论已更新 impact_calib.id={rid}（同区间已有留档，未新增行）")
    else:
        execute(
            """INSERT INTO impact_calib
                 (calib_date, n_orders, n_trades, period_start, period_end,
                  base_bps, impact_coef, r_squared, method, note)
               VALUES
                 (:d, :no, :nt, :ps, :pe, :bb, :ic, :r2, :m, :note)""",
            {"d": datetime.now().date(), "no": 0, "nt": int(result["n"]),
             "ps": ps, "pe": pe, "bb": result["base_bps"],
             "ic": result["impact_coef"], "r2": result["r_squared"],
             "m": result["method"], "note": note},
        )
        logger.info("回归结论已写入 impact_calib 表")


def prune_duplicates(dry_run: bool = False) -> int:
    """删除 impact_calib 中内容完全重复的冗余行，保留 id 最小的一条。"""
    df = read_sql(
        """SELECT id, period_start, period_end, method,
                  ROUND(base_bps, 6) AS b, ROUND(impact_coef, 6) AS ic,
                  ROUND(r_squared, 6) AS r2
             FROM impact_calib ORDER BY id"""
    )
    if df.empty:
        print("impact_calib 为空，无需清理")
        return 0

    keys = ["period_start", "period_end", "method", "b", "ic", "r2"]
    keep: Dict[tuple, int] = {}
    dups: List[int] = []
    for _, r in df.iterrows():
        k = tuple(str(r[c]) for c in keys)
        if k in keep:
            dups.append(int(r["id"]))
        else:
            keep[k] = int(r["id"])

    if not dups:
        print("无重复记录")
        return 0
    print(f"重复记录 {len(dups)} 条（将删除）: id={dups}，保留 id={sorted(keep.values())}")
    if dry_run:
        print("[--dry-run] 未执行删除")
        return len(dups)

    ids = ", ".join(str(i) for i in dups)
    execute(f"DELETE FROM impact_calib WHERE id IN ({ids})")
    logger.info(f"已清理 impact_calib 重复记录 {len(dups)} 条")
    return len(dups)


# ================================================================ 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="impact_coef 实证校准")
    ap.add_argument("--regress", action="store_true", help="执行回归")
    ap.add_argument("--save", action="store_true", help="回归结论写入 impact_calib 表")
    ap.add_argument("--write-back", action="store_true",
                    help="回归结论回写 costs.yaml（真实成交样本才允许）")
    ap.add_argument("--force", action="store_true",
                    help="强制回写（模拟样本专用，配置内会标注非实证）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要改动的内容")
    ap.add_argument("--prune", action="store_true", help="清理 impact_calib 重复记录")
    ap.add_argument("--start", default=None, help="样本起始日期")
    ap.add_argument("--end", default=None, help="样本结束日期")
    ap.add_argument("--min-samples", type=int, default=None,
                    help="最小样本量（默认取 execution.yaml）")
    args = ap.parse_args()

    if args.prune:
        prune_duplicates(dry_run=args.dry_run)
        return 0

    cfg = get_config("execution")
    imp_cfg = cfg.get("impact", {}) or {}
    store = TradeStore()

    df = load_trades_with_amount(
        store, start=args.start, end=args.end,
        participation_cap=float(imp_cfg.get("participation_cap", 0.10)),
    )
    if df.empty:
        logger.warning("无成交记录 —— 先接 QMT 模拟盘积累，或跑模拟撮合造样本")
        return 1

    df = compute_slip_bps(df)
    summ = summarize(df)

    # 样本来源判定：决定结论能否回写 costs.yaml
    src, dist = sample_source(df)
    period = f"{args.start or '全部'} ~ {args.end or '全部'}"

    print("\n" + "=" * 60)
    print("  成交样本概况")
    print("=" * 60)
    for k, v in summ.items():
        if k == "by_side":
            continue
        if isinstance(v, float) and v is not None:
            print(f"  {k:20s} {v:>12.2f}")
        else:
            print(f"  {k:20s} {str(v):>12s}")
    if summ.get("by_side"):
        print("\n  分方向滑点(bps):")
        for side, agg in summ["by_side"].items():
            print(f"    {side:6s} 均值 {agg['mean']:>8.2f}  样本 {int(agg['count'])}")
    print(f"\n  样本来源: {_SRC_LABEL.get(src, src)}   分布 {dist}")
    if src == "simulated":
        print(f"  ⚠ {_FORCE_WARN}")
        print("    → 该结论只能证明回归链路可用，不可回写 costs.yaml 冒充实证值")
    print("=" * 60)

    if not args.regress:
        print("\n加 --regress 执行回归；--save 留档；--write-back 回写 costs.yaml")
        return 0

    min_samples = args.min_samples or int(imp_cfg.get("min_samples", 30))
    result = regress_impact(df, min_samples=min_samples)
    print("\n" + "=" * 60)
    print("  回归结论（slip_bps = base + impact*sqrt(participation)*100）")
    print("=" * 60)
    if result.get("impact_coef") is None:
        print(f"  样本不足（{result.get('n')} < {min_samples}），继续积累成交记录")
        return 1
    print(f"  样本数      {result['n']}   （来源: {_SRC_LABEL.get(src, src)}）")
    print(f"  base_bps    {result['base_bps']:.4f}")
    print(f"  impact_coef {result['impact_coef']:.6f}")
    print(f"  R²          {result['r_squared']:.4f}")
    print("=" * 60)

    auto_wb = bool(imp_cfg.get("auto_write_back", False))
    do_write = args.write_back or auto_wb

    # ---------------- 回写 costs.yaml（受样本来源门控）----------------
    # 门控必须先于 --save 落库判定 —— 否则模拟样本会写入 note="已回写" 而
    # 实际被拒，impact_calib 表状态与 costs.yaml 永久不一致（撒谎）。
    write_state = "未回写"            # 默认：没请求回写
    if do_write and src != "real" and not args.force:
        write_state = "拒绝(模拟样本)"
        logger.error(
            f"拒绝回写：回归样本来自模拟撮合（broker 分布 {dist}），"
            f"不含真实市场冲击信息。\n"
            f"  该系数 {result['impact_coef']:.4f} 只是对造数真值的估计误差，"
            f"回写会让配置看起来'已实证'实则仍是拍脑袋值。\n"
            f"  若确为链路自检需要写入，请显式加 --force"
            f"（配置内会强制标注非实证）。"
        )
        if not args.save:
            return 2
    elif do_write:
        # 允许回写：真实执行（非 dry-run）后才标"已回写"
        if not args.dry_run:
            ok = write_back_costs(result, src, dist, period, dry_run=False)
            write_state = "已回写" if ok else "回写失败"
            if ok and src == "real":
                print("\n  提示：已用真实成交完成实证校准，可考虑下调 "
                      "tiers.conservative.slip_multiplier（原 1.5 是为覆盖未实证风险）")
            if not ok:
                return 1
        else:
            ok = write_back_costs(result, src, dist, period, dry_run=True)
            write_state = "dry-run(未写入)"
            if not ok:
                return 1

    if args.save:
        note = (f"auto_write_back={str(auto_wb).lower()}; "
                f"样本来源={src}; 回写状态={write_state}")
        upsert_calib(result, args.start, args.end, note)

    if write_state == "拒绝(模拟样本)":
        return 2
    if not do_write:
        print("\n  未回写 costs.yaml。要回写请加 --write-back，"
              "或在 execution.yaml 设 impact.auto_write_back: true")
        print(f"    slippage.impact_coef: {result['impact_coef']:.6f}")
        print(f"    slippage.base_bps:    {result['base_bps']:.4f}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
6指标估值模型 V3 — 流水线编排

  Stage 1  拉全A快照（新浪）
  Stage 2  市值/PE 预筛 → 取前 N 只 → 并发拉财务 → 九条件粗筛
  Stage 3  写 screen_pool2.md（粗筛池，供复核）
  Stage 4  DCF + 格林厄姆估值
  Stage 5  六指标打分排序
  Stage 6  硬门槛过滤
  Stage 7  输出 .md / .xlsx / 选股结果.json
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import datasource as ds
import model as md
from reporter import write_json_result, write_markdown, write_xlsx

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = "/workspace/报告"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def stage_banner(n, title):
    log(f"───── Stage {n}: {title} ─────")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--limit", type=int, default=0, help="候选上限，0=用配置值")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="只跑粗筛，不做估值")
    args = ap.parse_args()

    t_start = time.time()
    cfg = md.load_config()
    max_n = args.limit or cfg["screen"]["max_candidates"]
    workers = args.workers or cfg["datasource"]["concurrency"]

    os.makedirs(REPORT_DIR, exist_ok=True)
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)

    stats = {"failed_stocks": [], "errors": []}

    # ---------------- Stage 1
    stage_banner(1, "拉取全A股快照")
    try:
        universe = ds.fetch_all_a(progress=lambda n, p: log(f"  已抓 {n} 只（第 {p} 页）") if p % 10 == 0 else None)
    except Exception as e:  # noqa: BLE001
        log(f"✗ 全A快照拉取失败: {e}")
        return 2
    log(f"✓ 全A {len(universe)} 只")

    # ---------------- Stage 2
    stage_banner(2, "市值/PE 预筛 + 财务采集 + 九条件粗筛")
    s = cfg["screen"]
    pre = []
    for q in universe:
        if q["symbol"][:2] in s["exclude_boards"]:
            continue
        if s["exclude_st"] and ("ST" in q["name"].upper() or "退" in q["name"]):
            continue
        mcap_yi = (q.get("market_cap") or 0) / 1e4
        if mcap_yi <= s["min_market_cap_yi"]:
            continue
        pe = q.get("pe_ttm")
        if pe is None or pe <= s["min_pe"]:
            continue
        pre.append(q)
    pre.sort(key=lambda x: x["market_cap"], reverse=True)
    log(f"  预筛（市值>{s['min_market_cap_yi']}亿 + PE>0）通过 {len(pre)} 只")

    targets = pre[:max_n]
    log(f"  取市值前 {len(targets)} 只采集财务（预计 {len(targets)} 次请求）")

    fins = ds.fetch_finance_batch(
        [q["symbol"] for q in targets], workers=workers,
        progress=lambda d, t: log(f"  财务进度 {d}/{t}"),
    )
    ok_fin = sum(1 for v in fins.values() if v)
    log(f"✓ 财务采集成功 {ok_fin}/{len(targets)}")

    pool = []
    rejected = []
    for q in targets:
        raw = fins.get(q["symbol"])
        if raw is None:
            stats["failed_stocks"].append({"code": q["code"], "name": q["name"], "reason": "财务请求失败"})
            continue
        fin = md.parse_financials(raw)
        passed, fails = md.prefilter_stock(q, fin, cfg)
        if passed:
            q["fin"] = fin
            pool.append(q)
        else:
            rejected.append({"code": q["code"], "name": q["name"], "reason": "；".join(fails)})

    log(f"✓ 粗筛通过 {len(pool)} 只，剔除 {len(rejected)} 只")

    # ---------------- Stage 3
    stage_banner(3, "生成粗筛池 screen_pool2.md")
    pool_path = os.path.join(BASE, "screen_pool2.md")
    with open(pool_path, "w", encoding="utf-8") as f:
        f.write(f"# 粗筛池 screen_pool2\n\n生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}\n")
        f.write(f"模型：{cfg['model_version']}　全A {len(universe)} 只 → 预筛 {len(pre)} → "
                f"精算 {len(targets)} → 通过 {len(pool)}\n\n")
        f.write("| # | 代码 | 名称 | 市值(亿) | PE | EPS | ROE3Y(%) | 净利CAGR(%) | 资产负债率(%) |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        for i, q in enumerate(pool, 1):
            fin = q["fin"]
            roes = [a["roe"] for a in fin["annuals"][:3] if a["roe"] is not None]
            roe3 = sum(roes) / len(roes) if roes else None
            f.write(f"| {i} | {q['code']} | {q['name']} | {q['market_cap']/1e4:.0f} | "
                    f"{q['pe_ttm']:.1f} | {fin['eps']:.2f} | "
                    f"{(f'{roe3:.1f}' if roe3 else '-')} | - | {fin['debt_ratio']:.1f} |\n")
    log(f"✓ 已写入 {pool_path}")

    if args.dry_run:
        log("--dry-run 指定，跳过估值")
        return 0

    # ---------------- Stage 4
    stage_banner(4, "DCF + 格林厄姆估值")
    valued = []
    for q in pool:
        fin = q["fin"]
        shares = None
        if q.get("market_cap") and q.get("price"):
            shares = q["market_cap"] * 1e4 / q["price"]  # 万元->元 / 元/股

        dcf_v, dcf_detail, dcf_err = md.calc_dcf(fin, shares, cfg)
        if dcf_v is None:
            stats["failed_stocks"].append({"code": q["code"], "name": q["name"],
                                           "reason": f"DCF: {dcf_err}"})
            continue

        g_pct = dcf_detail["g_used"]
        graham_v, g_detail, g_err = md.calc_graham(fin, g_pct, cfg, price=q["price"], pe=q["pe_ttm"])
        if graham_v is None:
            stats["failed_stocks"].append({"code": q["code"], "name": q["name"],
                                           "reason": f"格林厄姆: {g_err}"})
            continue

        price = q["price"]
        roes = [a["roe"] for a in fin["annuals"][:3] if a["roe"] is not None]

        it = {
            "code": ds.to_secucode(q["symbol"]),
            "name": q["name"],
            "price": round(price, 2),
            "market_cap": round(q["market_cap"] / 1e4, 1),
            "pe_ttm": q["pe_ttm"],
            "pb": q["pb"],
            "eps": fin["eps"],
            "bps": fin["bps"],
            "roe_3y": round(sum(roes) / len(roes), 2) if roes else None,
            "np_cagr": round(dcf_detail["g_raw"], 2),
            "debt_ratio": round(fin["debt_ratio"], 2),
            "revenue_yoy": fin["revenue_yoy"],
            "profit_yoy": fin["profit_yoy"],
            "deducted_profit_yoy": fin["deducted_profit_yoy"],
            "dcf_value": round(dcf_v, 2),
            "graham_value": round(graham_v, 2),
            "dcf_discount": round((dcf_v - price) / dcf_v * 100, 2) if dcf_v > 0 else None,
            "graham_discount": round((graham_v - price) / graham_v * 100, 2) if graham_v > 0 else None,
            "report_date": fin["report_date"],
            "g_used": round(g_pct, 2),
        }
        valued.append(it)

    log(f"✓ 完成估值 {len(valued)} 只（失败 {len(pool)-len(valued)} 只）")

    # ---------------- Stage 5
    stage_banner(5, "六指标打分排序")
    md.score_pool(valued, cfg)
    valued.sort(key=lambda x: x["score"], reverse=True)
    log(f"✓ 打分完成，最高分 {valued[0]['score'] if valued else 0}")

    # ---------------- Stage 6
    stage_banner(6, "硬门槛过滤")
    for it in valued:
        md.apply_threshold(it, cfg)
    passed = [x for x in valued if x.get("pass")]
    log(f"✓ 通过安全边际 {len(passed)} 只")

    # ---------------- Stage 7
    stage_banner(7, "输出报告")
    ctx = {
        "date": args.date,
        "universe": len(universe),
        "prefiltered": len(pre),
        "targets": len(targets),
        "pool": len(pool),
        "valued": valued,
        "passed": passed,
        "rejected": rejected,
        "stats": stats,
        "elapsed": time.time() - t_start,
        "cfg": cfg,
    }

    md_path = os.path.join(REPORT_DIR, f"{args.date}_选股筛选_估值模型.md")
    xlsx_path = os.path.join(REPORT_DIR, f"{args.date}_选股筛选_估值模型.xlsx")
    write_markdown(md_path, ctx)
    write_xlsx(xlsx_path, ctx)
    write_json_result("/workspace/选股结果.json", ctx)

    log(f"✓ 报告: {md_path}")
    log(f"✓ 表格: {xlsx_path}")
    log(f"✓ 结果: /workspace/选股结果.json")
    log(f"总耗时 {time.time()-t_start:.1f}s | 全A {len(universe)} → 粗筛 {len(pool)} → "
        f"估值 {len(valued)} → 通过 {len(passed)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("中断")
        sys.exit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)

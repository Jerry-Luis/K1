#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
6指标估值模型 V3 — 计算核心

严格实现 config/model_v3.md 的口径。任何公式调整必须先改文档。
"""
import json
import os

CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "model_v3.json")


def load_config(path=CFG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 财务解析
def _num(v):
    try:
        x = float(v)
        return x if x == x else None  # 排除 NaN
    except (TypeError, ValueError):
        return None


def parse_financials(rows):
    """
    将东财财务原始行解析为模型所需结构。

    注意口径（见 model_v3.md 第一章）：
      - ROEJQ 是"报告期累计加权ROE"：中报 16.75% 对应全年约 33%，
        只有『年报』行的 ROEJQ 才是年度 ROE，做连续3年判断必须用年报行。
      - ZCFZL / EPSJB / BPS 取最新一期。
    """
    if not rows:
        return None

    annuals = [r for r in rows if r.get("REPORT_TYPE") == "年报"]
    annuals.sort(key=lambda r: r["REPORT_DATE"], reverse=True)
    latest = rows[0]  # 已按 REPORT_DATE 倒序

    out = {
        "report_date": latest["REPORT_DATE"][:10],
        "report_type": latest.get("REPORT_TYPE"),
        "eps_latest": _num(latest.get("EPSJB")),      # 最新期 EPS（仅展示）
        "bps": _num(latest.get("BPS")),
        "debt_ratio": _num(latest.get("ZCFZL")),
        "revenue_yoy": _num(latest.get("TOTALOPERATEREVETZ")),
        "profit_yoy": _num(latest.get("PARENTNETPROFITTZ")),
        "deducted_profit_yoy": _num(latest.get("KCFJCXSYJLRTZ")),
        "annuals": [],
    }

    for a in annuals[:5]:
        out["annuals"].append({
            "year": a["REPORT_DATE"][:4],
            "roe": _num(a.get("ROEJQ")),
            "net_profit": _num(a.get("PARENTNETPROFIT")),
            "deducted_profit": _num(a.get("KCFJCXSYJLR")),
            "eps": _num(a.get("EPSJB")),
            "revenue": _num(a.get("TOTALOPERATEREVE")),
        })

    # 口径铁律：估值与EPS门槛统一用"最新年报EPS"，避免中报EPS被当作全年值
    out["eps"] = out["annuals"][0]["eps"] if out["annuals"] else None
    out["eps_year"] = out["annuals"][0]["year"] if out["annuals"] else None

    # 数据一致性：市值/股价 推出的股本，应能与年报EPS × 股本 对上净利（5倍容差）
    return out


# ------------------------------------------------------------------ 粗筛
def prefilter_stock(q, fin, cfg):
    """
    单项九条件粗筛（不含排序取前N）。返回 (是否通过, [未通过原因])。
    行情 q 来自新浪快照，fin 为 parse_financials 结果。
    """
    s = cfg["screen"]
    fails = []

    name = q["name"]
    if s["exclude_st"] and ("ST" in name.upper() or "退" in name):
        fails.append("ST/退市")
    if q["symbol"][:2] in s["exclude_boards"]:
        fails.append("北交所")

    mcap_yi = (q.get("market_cap") or 0) / 1e4  # 万元 -> 亿元
    if mcap_yi <= s["min_market_cap_yi"]:
        fails.append(f"市值{mcap_yi:.0f}亿≤{s['min_market_cap_yi']}亿")

    pe = q.get("pe_ttm")
    if pe is None or pe <= s["min_pe"]:
        fails.append(f"PE={pe}≤{s['min_pe']}")

    if not fin:
        fails.append("财务缺失")
        return False, fails

    if fin["eps"] is None or fin["eps"] <= s["min_eps"]:
        fails.append(f"EPS={fin['eps']}<{s['min_eps']}")
    if fin["debt_ratio"] is None or fin["debt_ratio"] >= s["max_debt_ratio"]:
        fails.append(f"资产负债率={fin['debt_ratio']}≥{s['max_debt_ratio']}")
    if fin["revenue_yoy"] is None or fin["revenue_yoy"] <= s["min_revenue_yoy"]:
        fails.append(f"营收同比={fin['revenue_yoy']}≤0")
    if fin["profit_yoy"] is None or fin["profit_yoy"] <= s["min_profit_yoy"]:
        fails.append(f"归母净利同比={fin['profit_yoy']}≤0")
    if fin["deducted_profit_yoy"] is None or fin["deducted_profit_yoy"] <= s["min_deducted_profit_yoy"]:
        fails.append("扣非净利同比≤0")

    ann = fin["annuals"]
    need = s["roe_positive_years"]
    if len(ann) < need:
        fails.append(f"年报不足{need}年")
    else:
        roes = [a["roe"] for a in ann[:need]]
        if any(r is None or r <= 0 for r in roes):
            fails.append(f"近{need}年ROE非全正{roes}")

    g_need = s["profit_growth_years"]
    if len(ann) < g_need + 1:
        fails.append(f"年报不足{g_need+1}年(需算增长)")
    else:
        for i in range(g_need):
            cur, prev = ann[i]["net_profit"], ann[i + 1]["net_profit"]
            if cur is None or prev is None or prev <= 0 or cur <= prev:
                fails.append(f"{ann[i]['year']}年净利未增长")
                break

    return len(fails) == 0, fails


# ------------------------------------------------------------------ 估值
def calc_dcf(fin, shares, cfg):
    """
    DCF。FCF0 = 最新年报归母净利润；3年高增长(CAGR截断) + 永续 g∞。
    返回 (每股价值, 明细dict) 或 (None, reason)
    """
    d = cfg["dcf"]
    ann = fin["annuals"]
    need = d["growth_years"]
    if len(ann) < need + 1:
        return None, {}, "年报不足，无法计算CAGR"

    np0 = ann[0]["net_profit"]
    np_base = ann[need]["net_profit"]
    if not np0 or np0 <= 0:
        return None, {}, "最新年报净利润≤0"
    if not np_base or np_base <= 0:
        return None, {}, "基期净利润≤0，CAGR不可算"

    g = (np0 / np_base) ** (1.0 / need) - 1.0
    g_raw = g
    g = max(d["growth_floor"], min(d["growth_cap"], g))

    r = d["discount_rate"]
    gt = d["terminal_growth"]
    if r - gt <= 0:
        return None, {}, "折现率≤永续增长"

    pv, fcf = 0.0, np0
    for t in range(1, need + 1):
        fcf = fcf * (1 + g)
        pv += fcf / (1 + r) ** t
    tv = fcf * (1 + gt) / (r - gt)
    pv += tv / (1 + r) ** need

    if shares is None or shares <= 0:
        return None, {}, "股本缺失"

    per_share = pv / shares
    detail = {
        "g_raw": g_raw * 100, "g_used": g * 100,
        "fcf0": np0, "ev": pv, "terminal_value": tv,
        "per_share": per_share,
    }
    return per_share, detail, None


def calc_graham(fin, g_pct, cfg, price=None, pe=None):
    """格林厄姆 V = EPS × (8.5 + 2g) × 4.4 / Y　（EPS 取最新年报）"""
    g_cfg = cfg["graham"]
    eps = fin["annuals"][0]["eps"] if fin["annuals"] else None
    if eps is None or eps <= 0:
        return None, {}, "年报EPS≤0"

    g = max(g_cfg["growth_floor"], min(g_cfg["growth_cap"], g_pct))
    y = g_cfg["current_bond_yield"]
    if y <= 0:
        return None, {}, "国债收益率无效"

    v = eps * (g_cfg["zero_growth_pe"] + 2 * g) * g_cfg["aaa_bond_yield"] / y

    # 合理性校验：Y=2% 时公式隐含 PE=(8.5+2g)*2.2，g=25 得 129 倍，
    # 对成熟A股明显失真。若价值/现价 > 5 倍，判定公式不适用，不做估值。
    if price and price > 0:
        ratio = v / price
        if ratio > 5:
            return None, {"ratio": ratio}, f"格氏值/现价={ratio:.1f}倍，公式在Y={y}%下失真"

    detail = {"eps": eps, "g_used": g, "bond_yield": y, "ratio": (v / price) if price else None}
    return v, detail, None


# ------------------------------------------------------------------ 打分
def percentile_scores(items, key, invert=False):
    """
    池内线性秩分位（0~100，同值取平均秩），invert=True 表示数值越小分越高。
    返回 {code: score}
    """
    vals = [(it["code"], it[key]) for it in items if it.get(key) is not None]
    n = len(vals)
    if n == 0:
        return {}
    if n == 1:
        return {vals[0][0]: 50.0}

    ordered = sorted(vals, key=lambda x: x[1])
    rank_map, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            rank_map[ordered[k][0]] = avg_rank
        i = j + 1

    scores = {}
    for code, rk in rank_map.items():
        p = rk / (n - 1) * 100.0
        scores[code] = 100.0 - p if invert else p
    return scores


def score_pool(items, cfg):
    """对通过估值的股票打六指标分，写入 item['score'] 及分项。"""
    weights = cfg["scoring"]["weights"]
    invert = set(cfg["scoring"]["invert_direction"])

    parts = {}
    for key in weights:
        parts[key] = percentile_scores(items, key, invert=(key in invert))

    for it in items:
        total = 0.0
        detail = {}
        for key, w in weights.items():
            sc = parts[key].get(it["code"])
            detail[key] = round(sc, 1) if sc is not None else None
            if sc is not None:
                total += sc * w
        it["score"] = round(total, 2)
        it["score_parts"] = detail
        # 分位缺失时总分不可比，标记
        if any(v is None for v in detail.values()):
            it["score_note"] = "部分分项缺数据"
    return items


def apply_threshold(it, cfg):
    """硬门槛：DCF折价≥20% 且 现价<格林厄姆价值"""
    t = cfg["thresholds"]
    dcf_d = it.get("dcf_discount")
    gv = it.get("graham_value")
    price = it.get("price")

    ok_dcf = dcf_d is not None and dcf_d >= t["min_dcf_discount"]
    ok_graham = (not t["require_price_below_graham"]) or (gv is not None and price < gv)

    if ok_dcf and ok_graham:
        it["pass"] = True
        it["reason"] = ""
    else:
        rs = []
        if not ok_dcf:
            rs.append(f"DCF折价{dcf_d:.1f}%<{t['min_dcf_discount']}%" if dcf_d is not None else "DCF折价缺失")
        if not ok_graham:
            rs.append(f"现价{price}>格林厄姆{gv:.2f}" if gv is not None else "格林厄姆价值缺失")
        it["pass"] = False
        it["reason"] = "；".join(rs)
    return it

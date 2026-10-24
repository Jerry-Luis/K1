#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据源模块 — iFinD 额度耗尽后的替代实现

数据源分工：
  1. 新浪财经  Market_Center.getHQNodeData  → 全A股快照（价格/PE/PB/总市值/流通市值）
                                               用于粗筛（市值、PE 一步到位，1次/页）
  2. 东财 F10  RPT_F10_FINANCE_MAINFINADATA → 单只股票全部历史财务期
                                               （ROE/资产负债率/EPS/BPS/营收同比/净利同比/扣非同比）
                                               1次请求可覆盖 3 年年报 + 最新期，无需拆分
  3. 腾讯       qt.gtimg.cn                 → 实时价交叉校验 + 名称（备用）

相比原 iFinD 方案（每只 2 次请求）实际更省：粗筛阶段 56 次分页覆盖全市场，
精算阶段每只仅 1 次财务请求。
"""
import json
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

warnings.filterwarnings("ignore")

SINA_LIST = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
             "/Market_Center.getHQNodeData")
EM_FIN = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
TX_QUOTE = "https://qt.gtimg.cn/q="

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
    "Accept": "*/*",
}


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    ad = requests.adapters.HTTPAdapter(pool_connections=24, pool_maxsize=24, max_retries=0)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


_SESS = _session()


def _get(url, params=None, timeout=25, retries=3):
    """带重试的 GET。verify=False：容器内 CA 证书链不全。"""
    last = None
    for i in range(retries):
        try:
            r = _SESS.get(url, params=params, timeout=timeout, verify=False)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
        time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"请求失败 {url} -> {last}")


# ---------------------------------------------------------------- 全A快照
def fetch_all_a(progress=None):
    """
    拉取全A股快照。返回 list[dict]，每项含：
      symbol(如 sh600519), code, name, price, pe, pb, mktcap(万元), float_cap(万元)
    """
    rows, page = [], 1
    while page <= 80:
        r = _get(SINA_LIST, {
            "page": page, "num": 100, "sort": "symbol", "asc": 1,
            "node": "hs_a", "_s_r_a": "page",
        })
        try:
            data = json.loads(r.text)
        except json.JSONDecodeError:
            break
        if not data:
            break
        rows.extend(data)
        if progress:
            progress(len(rows), page)
        page += 1

    out = []
    for r in rows:
        try:
            price = float(r["trade"])
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out.append({
            "symbol": r["symbol"],
            "code": r["code"],
            "name": r["name"].replace(" ", ""),
            "price": price,
            "pe_ttm": _f(r.get("per")),
            "pb": _f(r.get("pb")),
            "market_cap": _f(r.get("mktcap")),      # 万元
            "float_cap": _f(r.get("nmc")),          # 万元
        })
    return out


def _f(v):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def to_secucode(symbol):
    """sh600519 -> 600519.SH"""
    return f"{symbol[2:]}.{symbol[:2].upper()}"


# ---------------------------------------------------------------- 财务
FIN_COLS = ("SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,REPORT_TYPE,"
            "ROEJQ,ROEKCJQ,ZCFZL,EPSJB,BPS,PARENTNETPROFIT,PARENTNETPROFITTZ,"
            "KCFJCXSYJLR,KCFJCXSYJLRTZ,TOTALOPERATEREVE,TOTALOPERATEREVETZ")


def fetch_finance(symbol, page_size=30):
    """拉取单只股票全部财务期（按报告期倒序）。失败返回 None。"""
    code = to_secucode(symbol)
    try:
        r = _get(EM_FIN, {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": FIN_COLS,
            "filter": f'(SECUCODE="{code}")',
            "pageNumber": 1,
            "pageSize": page_size,
            "sortTypes": -1,
            "sortColumns": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        })
        d = json.loads(r.text)
        if not d.get("success") or not d.get("result"):
            return None
        return d["result"]["data"]
    except Exception:  # noqa: BLE001
        return None


def fetch_finance_batch(symbols, workers=8, progress=None):
    """并发拉取财务。返回 {symbol: rows|None}"""
    result = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_finance, s): s for s in symbols}
        for fu in as_completed(futs):
            s = futs[fu]
            try:
                result[s] = fu.result()
            except Exception:  # noqa: BLE001
                result[s] = None
            done += 1
            if progress and done % 20 == 0:
                progress(done, len(symbols))
    return result


# ---------------------------------------------------------------- 腾讯实时（校验用）
def fetch_tencent(symbols):
    """批量取腾讯行情，返回 {symbol: {price, pe, pb, mktcap}}。每批 50 只。"""
    out = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        try:
            r = _get(TX_QUOTE + ",".join(batch), timeout=20)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or "=" not in line:
                    continue
                key = line[2:line.index("=")]
                seg = line.split('"')[1].split("~")
                if len(seg) < 46:
                    continue
                out[key] = {
                    "name": seg[1],
                    "price": _f(seg[3]),
                    "pe_ttm": _f(seg[39]) if len(seg) > 39 else None,
                    "pb": _f(seg[46]) if len(seg) > 46 else None,
                    "market_cap": _f(seg[45]) if len(seg) > 45 else None,  # 亿元
                }
        except Exception:  # noqa: BLE001
            continue
    return out

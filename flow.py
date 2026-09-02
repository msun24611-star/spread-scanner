"""
flow.py —— 期权异动扫描(七大科技股 + SPY/QQQ)
================================================
口径说明(重要,别误读):
  本模块用的是**期权链快照**,每个合约只有「当日累计成交量」这一个量的字段。
  因此这里算的是**合约级异动**——哪个合约今天不正常地活跃——
  而**不是逐笔大单**。快照分不出主动买/主动卖,也看不到单笔多大。
  想看真正的逐笔大单,得走 get_option_trade_ticks 逐合约拉 tick,成本高得多,
  可以在本页选中某个合约后再做下钻(v2)。

异动怎么判:三个门槛同时满足才算
  1. volume  >= min_volume     绝对成交量,滤掉噪音
  2. vol/OI  >= min_vol_oi     今日成交 / 存量未平仓。>1 意味着今天的换手超过全部存量,
                               通常是新开仓而非平仓,这是最有信息量的一个比值。
  3. 名义金额 >= min_notional   volume × 中价 × 100,滤掉"量大但都是几分钱废纸"的合约

排序默认按名义金额(绝对值,可解释),不做归一化打分——
价差扫描器那套 0-100 分是结果集内相对分,放在异动这里容易误读成"异动强度绝对值"。
"""
import time
from datetime import date, datetime

from screener import _f, normalize_chain, underlying_price

# 七大科技股 + 两个大盘 ETF
FLOW_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "SPY", "QQQ"]

DEFAULTS = {
    "min_dte": 0,
    "max_dte": 45,
    "max_expiries": 6,        # 每个标的最多看几个到期日(控 API 调用量)
    "min_volume": 500,        # 当日成交量下限
    "min_vol_oi": 2.0,        # 成交/未平仓 下限
    "min_notional": 100000,   # 名义金额下限(美元)
    "top_per_symbol": 8,      # 每个标的最多返回几条
    "new_oi_threshold": 10,   # OI 低于此值视为"新行权价",vol/OI 失真,单独标注
}

CONTRACT_MULT = 100


def _expiries(symbol, p):
    """近月到期日,按 dte 升序取前 max_expiries 个。"""
    from screener import list_expirations
    exps = [e for e in list_expirations(symbol)
            if p["min_dte"] <= e["dte"] <= p["max_dte"]]
    return exps[: p["max_expiries"]]


def _price_of(leg):
    """成交均价的近似:优先中价,中价不可用退回最新价。"""
    bid, ask = leg.get("bid") or 0.0, leg.get("ask") or 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return leg.get("last") or 0.0


def _rows_for_expiry(symbol, S, exp, p):
    chain = normalize_chain(symbol, exp["date"])
    out = []
    for leg in chain:
        vol = leg.get("volume") or 0.0
        if vol < p["min_volume"]:
            continue
        oi = leg.get("oi") or 0.0
        px = _price_of(leg)
        if px <= 0:
            continue
        notional = vol * px * CONTRACT_MULT
        if notional < p["min_notional"]:
            continue
        vol_oi = vol / max(oi, 1.0)
        if vol_oi < p["min_vol_oi"]:
            continue
        strike = leg["strike"]
        out.append({
            "symbol": symbol,
            "price": round(S, 2),
            "expiry": exp["date"],
            "dte": exp["dte"],
            "right": leg["right"],                       # call / put
            "strike": strike,
            "moneyness": round(strike / S - 1, 4),        # 相对现价的价外幅度,正=行权价在上方
            "otm": (strike > S) if leg["right"] == "call" else (strike < S),
            "volume": int(vol),
            "oi": int(oi),
            "vol_oi": round(vol_oi, 2),
            "new_strike": oi < p["new_oi_threshold"],     # OI 太薄,vol/OI 参考价值下降
            "avg_px": round(px, 3),
            "notional": int(notional),
            "iv": leg.get("iv"),
            "delta": leg.get("delta"),
        })
    return out


def scan_symbol(symbol: str, params: dict = None):
    p = {**DEFAULTS, **(params or {})}
    symbol = symbol.upper().strip()
    S = underlying_price(symbol)
    if not S:
        return {"symbol": symbol, "error": "拿不到标的现价(可能没有美股行情权限)", "rows": []}

    rows, used = [], []
    for e in _expiries(symbol, p):
        try:
            got = _rows_for_expiry(symbol, S, e, p)
            used.append({"date": e["date"], "dte": e["dte"], "hits": len(got)})
            rows.extend(got)
        except Exception as ex:
            used.append({"date": e["date"], "error": str(ex)})

    rows.sort(key=lambda r: r["notional"], reverse=True)

    # 汇总:call/put 名义金额分布。注意这只是"哪边成交金额大",
    # 不等于"多空",因为看不出是买入开仓还是卖出开仓。
    call_notional = sum(r["notional"] for r in rows if r["right"] == "call")
    put_notional = sum(r["notional"] for r in rows if r["right"] == "put")
    total = call_notional + put_notional

    return {
        "symbol": symbol,
        "price": round(S, 2),
        "expirations": used,
        "count": len(rows),
        "call_notional": call_notional,
        "put_notional": put_notional,
        "call_share": round(call_notional / total, 4) if total else None,
        "total_notional": total,
        "rows": rows[: p["top_per_symbol"]],
    }


# ---------------------------------------------------------------- 方向判断(逐笔)
"""
方向怎么判——以及它的边界在哪
--------------------------------------------------
老虎的 get_option_trade_ticks 只给 time / price / volume,
**没有买卖方向标志,也没有成交当时的买卖盘价**。所以用 tick rule 自己推:
    涨价成交 = 主动买(买方吃卖一)
    跌价成交 = 主动卖(卖方砸买一)
    平价成交 = 沿用上一笔的方向
这是 Lee-Ready 的简化版,在期权上噪音不小。实测下来:
流动性好的平值合约基本都是 50/50(做市商双边对倒),没有信息量;
只有少数合约会出现一边倒——那才是有人在单向下注。
因此这里**不对每个合约都给方向结论**,只在成交量够大且分布够偏时才给,
其余一律标"中性",宁可不说也不瞎说。

方向映射(这一步才是"看涨看跌"的真正来源):
    主动买 call = 看涨      主动卖 call = 看跌
    主动买 put  = 看跌      主动卖 put  = 看涨
注意卖 call / 卖 put 也可能是备兑或保护腿的一部分,单腿数据看不出组合。
"""

DIR_DEFAULTS = {
    "min_flow_volume": 200,   # 可判方向所需的最小已分类成交量(张)
    "decisive_share": 0.65,   # 买或卖一边占比达到多少才算"压倒"
    "big_lot": 50,            # 单笔多少张算大单
    "top_contracts": 8,       # 每个标的分析前几个异动合约
    "min_coverage": 0.30,     # 判得出方向的成交金额需占多少,才配给一个方向结论
}


def occ_identifier(symbol: str, expiry: str, right: str, strike: float) -> str:
    """拼 OCC 合约代码,如 NVDA 2026-09-02 220 call -> 'NVDA  260902C00220000'。"""
    yy = expiry[2:4] + expiry[5:7] + expiry[8:10]
    return "%-6s%s%s%08d" % (symbol.upper(), yy, right[0].upper(), round(strike * 1000))


def _classify(prices, vols, big_lot):
    """tick rule 分类,返回 (主动买量, 主动卖量, 大单买量, 大单卖量, 大单笔数, 单笔最大)。"""
    buy = sell = big_buy = big_sell = 0
    big_n = 0
    max_lot = 0
    last = 0
    for i, (p, v) in enumerate(zip(prices, vols)):
        if i == 0:
            d = 0
        elif p > prices[i - 1]:
            d = 1
        elif p < prices[i - 1]:
            d = -1
        else:
            d = last
        if d:
            last = d
        v = int(v)
        max_lot = max(max_lot, v)
        if d > 0:
            buy += v
        elif d < 0:
            sell += v
        if v >= big_lot:
            big_n += 1
            if d > 0:
                big_buy += v
            elif d < 0:
                big_sell += v
    return buy, sell, big_buy, big_sell, big_n, max_lot


def analyze_contract(row, p):
    """对一条异动记录拉逐笔并判方向。row 来自 scan_symbol 的 rows。"""
    from tiger import quote
    ident = occ_identifier(row["symbol"], row["expiry"], row["right"], row["strike"])
    df = quote().get_option_trade_ticks([ident])
    n = len(df)
    out = {**row, "identifier": ident, "ticks": n}
    if not n:
        return {**out, "bias": "unknown", "reason": "没有逐笔数据"}

    buy, sell, big_buy, big_sell, big_n, max_lot = _classify(
        df["price"].tolist(), df["volume"].tolist(), p["big_lot"])
    tot = buy + sell
    out.update({
        "buy_vol": buy, "sell_vol": sell,
        "buy_share": round(buy / tot, 4) if tot else None,
        "big_trades": big_n, "big_buy": big_buy, "big_sell": big_sell,
        "max_lot": max_lot, "tick_volume": int(df["volume"].sum()),
    })

    if tot < p["min_flow_volume"]:
        return {**out, "bias": "neutral", "reason": "成交量不够,判不了"}
    share = buy / tot
    if share >= p["decisive_share"]:
        side = "buy"
    elif (1 - share) >= p["decisive_share"]:
        side = "sell"
    else:
        return {**out, "bias": "neutral", "reason": "买卖接近对半,多半是做市商对倒"}

    # 买 call / 卖 put = 看涨;卖 call / 买 put = 看跌
    bullish = (side == "buy") == (row["right"] == "call")
    return {**out,
            "bias": "bullish" if bullish else "bearish",
            "side": side,
            "reason": "%s%s %.0f%%" % ("主动买" if side == "buy" else "主动卖",
                                       "看涨期权" if row["right"] == "call" else "看跌期权",
                                       100 * (share if side == "buy" else 1 - share))}


def direction_for(symbol: str, params: dict = None):
    """对某标的的前 N 个异动合约逐一判方向,再汇总成标的层面的倾向。"""
    p = {**DEFAULTS, **DIR_DEFAULTS, **(params or {})}
    base = scan_symbol(symbol, p)
    if base.get("error"):
        return {"symbol": symbol.upper(), "error": base["error"], "contracts": []}

    rows = base["rows"][: p["top_contracts"]]
    analyzed, errors = [], []
    for r in rows:
        try:
            analyzed.append(analyze_contract(r, p))
        except Exception as ex:
            errors.append({"strike": r["strike"], "right": r["right"], "error": str(ex)})

    buckets = {"bullish": 0, "bearish": 0, "neutral": 0, "unknown": 0}
    for a in analyzed:
        buckets[a.get("bias", "unknown")] += a["notional"]
    decided = buckets["bullish"] + buckets["bearish"]
    undecided = buckets["neutral"] + buckets["unknown"]
    grand = decided + undecided

    # 覆盖率:判得出方向的成交金额占比。这一步不能省 ——
    # 只有 10% 的成交判得出方向、而这 10% 全是看涨时,说"偏看涨"是在虚张声势。
    coverage = decided / grand if grand else 0.0
    tilt = buckets["bullish"] / decided if decided else None

    if coverage < p["min_coverage"]:
        verdict = "信号不足"
    elif tilt >= 0.65:
        verdict = "偏看涨"
    elif tilt <= 0.35:
        verdict = "偏看跌"
    else:
        verdict = "多空混杂"

    return {
        "symbol": symbol.upper(),
        "price": base["price"],
        "contracts": analyzed,
        "errors": errors,
        "notional_by_bias": buckets,
        "decided_notional": decided,
        "undecided_notional": undecided,
        "coverage": round(coverage, 4),
        "bullish_share": round(tilt, 4) if tilt is not None else None,
        "verdict": verdict,
        "params": {k: p[k] for k in DIR_DEFAULTS},
    }


def scan_all(symbols: list = None, params: dict = None):
    """扫全部标的。按该标的异动总名义金额降序排列。"""
    p = {**DEFAULTS, **(params or {})}
    syms = symbols or FLOW_SYMBOLS
    groups, errors = [], []
    for sym in syms:
        try:
            res = scan_symbol(sym, p)
            if res.get("error"):
                errors.append({"symbol": sym, "error": res["error"]})
                continue
            groups.append(res)
        except Exception as ex:
            errors.append({"symbol": sym, "error": str(ex)})
    groups.sort(key=lambda g: g["total_notional"], reverse=True)
    return {
        "groups": groups,
        "errors": errors,
        "symbols": syms,
        "params": {k: p[k] for k in
                   ("min_dte", "max_dte", "min_volume", "min_vol_oi",
                    "min_notional", "top_per_symbol", "max_expiries")},
        "total_hits": sum(g["count"] for g in groups),
    }

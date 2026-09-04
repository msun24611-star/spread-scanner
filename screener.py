"""
screener.py —— 垂直信用价差筛选引擎
================================================
流程:拉期权链 → 枚举 Bull Put / Bear Call 信用价差 → 算 credit/maxLoss/RoR/POP/breakeven
→ 流动性/IV 指标 → 结果集内归一化的综合评分 → 排序。

v1 只出信用价差(卖方)。借方(Bull Call/Bear Put)预留:每条结果带 side 字段,
以后加 build_debit_spreads 即可,前端/评分无需改。

字段名兼容:老虎不同 SDK 版本期权链列名可能不同,_pick() 用多候选名兜底。
greeks 缺失时用 Black-Scholes 从 IV 兜底算 delta(POP)。
"""
import math
import time
from datetime import date, datetime, timedelta

from tiger import quote

# ---- 默认参数(前端可传覆盖) ----
DEFAULTS = {
    "min_dte": 1,
    "max_dte": 7,
    "short_delta_min": 0.15,   # 短腿 |delta| 下限(太虚值权利金太薄)
    "short_delta_max": 0.35,   # 短腿 |delta| 上限(太接近平值胜率低)
    "max_legs_away": 3,        # 长腿最多向外找几档
    "min_ror": 0.15,           # 最小回报风险比 credit/maxLoss
    "min_pop": 0.60,           # 最小胜率
    "min_oi": 50,              # 短腿最小未平仓
    "max_spread_pct": 0.30,    # 短腿买卖价差/中价 上限(流动性)
    "risk_free": 0.043,        # 无风险利率,BS 兜底用
    "top_n": 60,               # 每次返回上限
}

# 综合评分权重
WEIGHTS = {"pop": 0.35, "ror": 0.35, "iv": 0.15, "liq": 0.15}


# ---------------------------------------------------------------- 工具
def _pick(row: dict, *cands, default=None):
    """从一行 dict 里按候选列名依次取第一个存在且非空的值。"""
    for c in cands:
        if c in row and row[c] is not None and row[c] != "":
            return row[c]
    return default


def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_delta(right, S, K, T, iv, r):
    """Black-Scholes delta 兜底(当期权链没返回 delta 时)。right: 'call'/'put'。"""
    if not (S and K and T and iv) or T <= 0 or iv <= 0:
        return None
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    return _norm_cdf(d1) if right == "call" else _norm_cdf(d1) - 1.0


def _minmax(vals):
    lo, hi = min(vals), max(vals)
    rng = hi - lo
    return (lambda v: 0.5) if rng == 0 else (lambda v: (v - lo) / rng)


# ---------------------------------------------------------------- 数据获取
def underlying_price(symbol: str):
    df = quote().get_stock_briefs(symbols=[symbol])
    rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else list(df)
    if not rows:
        return None
    return _f(_pick(rows[0], "latest_price", "latestPrice", "last_price", "close", "pre_close"))


# ---------------------------------------------------------------- 财报日历
_earn_cache = {"ts": 0, "map": {}}  # symbol -> {"date","time"};全市场一次拉回,缓存 1 小时


def _refresh_earnings():
    from tigeropen.common.consts import Market
    today = date.today()
    end = today + timedelta(days=28)  # 接口限制:区间不能超过 1 个月
    m = {}
    try:
        df = quote().get_corporate_earnings_calendar(
            market=Market.US, begin_date=str(today), end_date=str(end))
        rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else list(df)
        for r in rows:
            sym = r.get("symbol")
            d = r.get("report_date")
            if not sym or not d:
                continue
            t = r.get("report_time")
            t = None if (t is None or (isinstance(t, float) and math.isnan(t))) else t
            # 同一标的取最近一次
            if sym not in m or d < m[sym]["date"]:
                m[sym] = {"date": d, "time": t}
    except Exception:
        pass
    _earn_cache["ts"] = time.time()
    _earn_cache["map"] = m


def earnings_for(symbol: str):
    """返回该标的未来 28 天内最近一次财报 {date,time},没有返回 None。全市场缓存 1 小时。"""
    if time.time() - _earn_cache["ts"] > 3600 or not _earn_cache["map"]:
        _refresh_earnings()
    return _earn_cache["map"].get(symbol.upper())


def list_expirations(symbol: str):
    """返回 [{date, timestamp, dte, period_tag}],按 dte 升序。"""
    df = quote().get_option_expirations(symbols=[symbol])
    rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else list(df)
    today = date.today()
    out = []
    for r in rows:
        d = _pick(r, "date")
        if not d:
            ts = _pick(r, "timestamp")
            if ts:
                d = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d")
        if not d:
            continue
        dte = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
        out.append({"date": d, "timestamp": _pick(r, "timestamp"),
                    "dte": dte, "period_tag": _pick(r, "period_tag")})
    return sorted(out, key=lambda x: x["dte"])


_WD = ["一", "二", "三", "四", "五", "六", "日"]


def expiry_cadence(symbol: str, horizon: int = 16):
    """看某标的近 horizon 天的到期日节奏(周几到期),给出频率标签。
    用于挑'每周一三五都有期权'的高频到期标的。"""
    exps = [e for e in list_expirations(symbol) if 0 <= e["dte"] <= horizon]
    days = []
    weekdays = set()
    for e in exps:
        wd = datetime.strptime(e["date"], "%Y-%m-%d").date().weekday()  # 0=周一
        weekdays.add(wd)
        days.append({"date": e["date"], "dte": e["dte"], "wd": f"周{_WD[wd]}"})
    biz = weekdays & {0, 1, 2, 3, 4}
    if len(biz) >= 5:
        label = "每日(日历期权)"
    elif {0, 2, 4} <= weekdays:
        label = "周一/三/五"
    elif {1, 3} <= weekdays:
        label = "周二/四"
    elif weekdays == {4}:
        label = "仅周五(标准周期权)"
    elif weekdays:
        label = "/".join(f"周{_WD[w]}" for w in sorted(biz))
    else:
        label = "近期无到期"
    return {"symbol": symbol.upper(), "label": label, "count": len(exps),
            "weekdays": sorted(biz), "expiries": days, "earnings": earnings_for(symbol)}


_CHAIN_CALL_TS = []          # option_chain 调用时间戳滑动窗口,跨 screener/flow/positions 共享
_CHAIN_LIMIT_PER_MIN = 55    # 老虎接口上限 60次/分钟,留点余量


def _throttle_chain_call():
    """把 option_chain 调用速率控制在滑动 60 秒窗口内 ≤_CHAIN_LIMIT_PER_MIN 次,
    避免一次扫描(如期权异动页 9 个标的 × 多个到期日)瞬间打爆老虎的限流。"""
    now = time.time()
    while _CHAIN_CALL_TS and now - _CHAIN_CALL_TS[0] > 60:
        _CHAIN_CALL_TS.pop(0)
    if len(_CHAIN_CALL_TS) >= _CHAIN_LIMIT_PER_MIN:
        wait = 60 - (now - _CHAIN_CALL_TS[0]) + 0.2
        if wait > 0:
            time.sleep(wait)
        now = time.time()
        while _CHAIN_CALL_TS and now - _CHAIN_CALL_TS[0] > 60:
            _CHAIN_CALL_TS.pop(0)
    _CHAIN_CALL_TS.append(now)


def _fetch_option_chain(symbol: str, expiry: str, retries: int = 2):
    """带限流节流 + 限流报错重试的 get_option_chain 包装。"""
    for attempt in range(retries + 1):
        _throttle_chain_call()
        try:
            return quote().get_option_chain(symbol=symbol, expiry=expiry, return_greek_value=True)
        except Exception as ex:
            if "rate limit" in str(ex).lower() and attempt < retries:
                time.sleep(3)
                continue
            raise


def normalize_chain(symbol: str, expiry: str):
    """拉一个到期日的期权链,标准化成统一字段的 list。
    统一字段: right('call'/'put'), strike, bid, ask, last, iv, delta, oi, volume。"""
    df = _fetch_option_chain(symbol, expiry)
    rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else list(df)
    out = []
    for r in rows:
        raw_right = str(_pick(r, "put_call", "right", "call_or_put", "type", default="")).lower()
        right = "call" if raw_right.startswith("c") else "put" if raw_right.startswith("p") else None
        if right is None:
            continue
        out.append({
            "right": right,
            "strike": _f(_pick(r, "strike", "strike_price")),
            "bid": _f(_pick(r, "bid_price", "bid"), 0.0),
            "ask": _f(_pick(r, "ask_price", "ask"), 0.0),
            "last": _f(_pick(r, "latest_price", "latestPrice", "last_price")),
            "iv": _f(_pick(r, "implied_vol", "implied_volatility", "iv")),
            "delta": _f(_pick(r, "delta")),
            "oi": _f(_pick(r, "open_interest", "openInterest"), 0.0),
            "volume": _f(_pick(r, "volume", "vol"), 0.0),
        })
    return [o for o in out if o["strike"] is not None]


# ---------------------------------------------------------------- 枚举价差
def _leg_delta(leg, S, T, r):
    d = leg.get("delta")
    if d is not None:
        return d
    return _bs_delta(leg["right"], S, leg["strike"], T, leg["iv"], r)


def _mk_spread(side, short, long, S, dte, p):
    """构造一条信用价差并算指标;不合格返回 None。"""
    width = abs(short["strike"] - long["strike"])
    if width <= 0:
        return None
    # 保守净收:卖短腿吃 bid,买长腿付 ask
    credit = round(short["bid"] - long["ask"], 4)
    mid_credit = round((short["bid"] + short["ask"]) / 2 - (long["bid"] + long["ask"]) / 2, 4)
    if credit <= 0:
        return None
    max_loss = round(width - credit, 4)
    if max_loss <= 0:
        return None
    ror = credit / max_loss
    T = max(dte, 0) / 365.0
    sd = _leg_delta(short, S, T, p["risk_free"])
    pop = None if sd is None else 1 - abs(sd)
    if side == "bull_put":
        breakeven = round(short["strike"] - credit, 4)
    else:  # bear_call
        breakeven = round(short["strike"] + credit, 4)
    # 流动性:短腿买卖价差占中价比例
    mid = (short["bid"] + short["ask"]) / 2
    spread_pct = (short["ask"] - short["bid"]) / mid if mid > 0 else 9.99
    return {
        "side": side,
        "strategy": "Bull Put(牛市看跌价差)" if side == "bull_put" else "Bear Call(熊市看涨价差)",
        "right": short["right"],
        "short_strike": short["strike"],
        "long_strike": long["strike"],
        "width": round(width, 4),
        "credit": credit,
        "mid_credit": mid_credit,
        "max_loss": max_loss,
        "ror": round(ror, 4),
        "pop": None if pop is None else round(pop, 4),
        "breakeven": breakeven,
        "short_delta": None if sd is None else round(sd, 4),
        "short_iv": short["iv"],
        "short_oi": short["oi"],
        "short_volume": short["volume"],
        "spread_pct": round(spread_pct, 4),
        "dte": dte,
    }


def build_credit_spreads(symbol, S, expiry, dte, chain, p):
    puts = sorted([c for c in chain if c["right"] == "put"], key=lambda x: x["strike"])
    calls = sorted([c for c in chain if c["right"] == "call"], key=lambda x: x["strike"])
    T = max(dte, 0) / 365.0
    results = []

    def short_ok(leg):
        if leg["bid"] <= 0 or leg["ask"] <= 0:
            return False
        if leg["oi"] < p["min_oi"]:
            return False
        d = _leg_delta(leg, S, T, p["risk_free"])
        if d is None:
            return False
        return p["short_delta_min"] <= abs(d) <= p["short_delta_max"]

    # Bull Put:短腿在现价下方(OTM put),长腿更低
    otm_puts = [x for x in puts if x["strike"] < S]
    for i, short in enumerate(otm_puts):
        if not short_ok(short):
            continue
        for long in otm_puts[max(0, i - p["max_legs_away"]):i]:  # 更低行权价
            sp = _mk_spread("bull_put", short, long, S, dte, p)
            if sp:
                results.append(sp)

    # Bear Call:短腿在现价上方(OTM call),长腿更高
    otm_calls = [x for x in calls if x["strike"] > S]
    for i, short in enumerate(otm_calls):
        if not short_ok(short):
            continue
        for long in otm_calls[i + 1:i + 1 + p["max_legs_away"]]:  # 更高行权价
            sp = _mk_spread("bear_call", short, long, S, dte, p)
            if sp:
                results.append(sp)
    return results


# ---------------------------------------------------------------- 评分 & 主入口
def _apply_filters_and_score(rows, p):
    kept = []
    for r in rows:
        if r["ror"] < p["min_ror"]:
            continue
        if r["pop"] is not None and r["pop"] < p["min_pop"]:
            continue
        if r["spread_pct"] > p["max_spread_pct"]:
            continue
        kept.append(r)
    if not kept:
        return []
    # 结果集内归一化
    pn = _minmax([r["pop"] if r["pop"] is not None else 0 for r in kept])
    rn = _minmax([r["ror"] for r in kept])
    ivn = _minmax([r["short_iv"] if r["short_iv"] is not None else 0 for r in kept])
    ln = _minmax([math.log1p(r["short_oi"]) for r in kept])
    for r in kept:
        s = (WEIGHTS["pop"] * pn(r["pop"] if r["pop"] is not None else 0)
             + WEIGHTS["ror"] * rn(r["ror"])
             + WEIGHTS["iv"] * ivn(r["short_iv"] if r["short_iv"] is not None else 0)
             + WEIGHTS["liq"] * ln(math.log1p(r["short_oi"])))
        r["score"] = round(s * 100, 1)
    kept.sort(key=lambda r: r["score"], reverse=True)
    return kept


def scan_symbol(symbol: str, params: dict = None):
    """扫单个标的,返回该标的全部合格价差(已评分排序)。"""
    p = {**DEFAULTS, **(params or {})}
    symbol = symbol.upper().strip()
    S = underlying_price(symbol)
    if not S:
        return {"symbol": symbol, "error": "拿不到标的现价(可能没有美股行情权限)", "spreads": []}
    exps = [e for e in list_expirations(symbol) if p["min_dte"] <= e["dte"] <= p["max_dte"]]
    earn = earnings_for(symbol)  # {date,time} 或 None
    all_rows = []
    used_exps = []
    for e in exps:
        try:
            chain = normalize_chain(symbol, e["date"])
        except Exception as ex:
            used_exps.append({"date": e["date"], "error": str(ex)})
            continue
        used_exps.append({"date": e["date"], "dte": e["dte"], "contracts": len(chain)})
        # 财报是否落在 今天~到期日 之间(持仓期内会撞财报)
        earn_in = bool(earn and str(date.today()) <= earn["date"] <= e["date"])
        for row in build_credit_spreads(symbol, S, e["date"], e["dte"], chain, p):
            row["symbol"] = symbol
            row["price"] = round(S, 2)
            row["expiry"] = e["date"]
            row["earnings_date"] = earn["date"] if earn else None
            row["earnings_time"] = earn["time"] if earn else None
            row["earnings_in_window"] = earn_in
            all_rows.append(row)
    scored = _apply_filters_and_score(all_rows, p)
    return {"symbol": symbol, "price": round(S, 2), "expirations": used_exps,
            "earnings": earn, "count": len(scored), "spreads": scored[: p["top_n"]]}


def scan_many(symbols: list, params: dict = None):
    """扫多个标的,合并后全局评分排序。"""
    p = {**DEFAULTS, **(params or {})}
    merged, errors = [], []
    for sym in symbols:
        try:
            res = scan_symbol(sym, params)
            if res.get("error"):
                errors.append({"symbol": sym, "error": res["error"]})
            merged.extend(res.get("spreads", []))
        except Exception as ex:
            errors.append({"symbol": sym, "error": str(ex)})
    scored = _apply_filters_and_score(merged, p)  # 全局重新归一化
    return {"count": len(scored), "errors": errors, "spreads": scored[: p["top_n"]]}

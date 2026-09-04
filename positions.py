"""
positions.py —— 观察 / 浮动盈亏 / 平仓建议
================================================
用户在价差扫描页点「加入观察」时,把那一行 spread 的入场快照存进 positions.json。
观察页每次打开调用 refresh(),用当前期权报价重新估值:

  浮动盈亏怎么算:
    开仓时:卖短腿收 credit,买长腿花权利金,净收 entry_credit。
    平仓时要做反向操作:买回短腿(付 ask)、卖出长腿(收 bid)。
    平仓净成本 cost_to_close = short_ask_now - long_bid_now。
    浮动盈亏 = (entry_credit - cost_to_close) × 100(每张合约)。
  这是用中价附近的可执行报价重新估值,不是券商账户里的真实持仓,仅供参考。

  平仓建议怎么给:见 _suggest() 的注释,是公式 + 期望值模型,不是投资建议。
"""
import json
import os
import secrets
import time
from datetime import date, datetime

from screener import _bs_delta, _f, normalize_chain, underlying_price

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "positions.json")

ENTRY_FIELDS = [
    "symbol", "side", "strategy", "right", "expiry",
    "short_strike", "long_strike", "width",
    "credit", "price", "dte", "pop", "short_delta", "short_iv",
]

# 平仓建议阈值
DANGER_DELTA = 0.60       # 短腿 |delta| 超过这个,视为实值概率大幅上升
LOCK_PROFIT_CAPTURED = 0.60  # 已经吃到手的权利金比例超过这个,建议锁盈
NEAR_EXPIRY_DTE = 3       # 剩余这么多天以内,gamma 风险优先于期望值


# ---------------------------------------------------------------- 存取
def _load():
    if not os.path.exists(STORE):
        return []
    with open(STORE, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save(rows):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _match_key(rec):
    return (rec.get("symbol"), rec.get("expiry"), rec.get("side"),
            rec.get("short_strike"), rec.get("long_strike"))


def add(spread: dict):
    """把一行 spread(来自 /api/scan)存成观察记录。已存在则原样返回,不重复加。"""
    rows = _load()
    key = _match_key(spread)
    for r in rows:
        if _match_key(r) == key and r.get("status") == "open":
            return {**r, "already_exists": True}

    rec = {"id": secrets.token_hex(4)}
    for k in ENTRY_FIELDS:
        rec[k] = spread.get(k)
    # 统一改名成 entry_* 前缀,避免跟 refresh() 算出来的"当前值"字段撞名
    rename = {"credit": "entry_credit", "price": "entry_price", "dte": "entry_dte",
              "pop": "entry_pop", "short_delta": "entry_short_delta", "short_iv": "entry_short_iv"}
    for old, new in rename.items():
        rec[new] = rec.pop(old)
    rec["added_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rec["status"] = "open"
    rows.append(rec)
    _save(rows)
    return {**rec, "already_exists": False}


def remove(pos_id: str):
    rows = _load()
    kept = [r for r in rows if r.get("id") != pos_id]
    removed = len(kept) != len(rows)
    if removed:
        _save(kept)
    return removed


# ---------------------------------------------------------------- 平仓建议
def _suggest(rec, cost_to_close, dte_now, delta_now, spot_now):
    """继续持有 vs 现在平仓,哪个更划算。透明公式,不是黑箱,reason 里说清楚。"""
    entry_credit = rec["entry_credit"]
    width = rec["width"]
    right = rec["right"]
    short_strike = rec["short_strike"]

    profit_captured = None if not entry_credit else round(1 - cost_to_close / entry_credit, 4)
    pop_now = None if delta_now is None else round(1 - abs(delta_now), 4)

    max_loss = width - entry_credit
    edge_of_holding = None
    if pop_now is not None:
        expected_if_hold = pop_now * entry_credit - (1 - pop_now) * max_loss
        locked_if_close_now = entry_credit - cost_to_close
        edge_of_holding = round(expected_if_hold - locked_if_close_now, 4)

    breach = (spot_now is not None) and (
        (right == "call" and spot_now > short_strike) or
        (right == "put" and spot_now < short_strike)
    )
    danger = breach or (delta_now is not None and abs(delta_now) >= DANGER_DELTA)

    if danger:
        verdict = "止损/展期"
        reason = (f"短腿{'已被击穿' if breach else f'|delta|已到 {abs(delta_now):.2f}'},"
                   "实值概率明显升高了。我会考虑现在平仓止损,或 roll 到下个到期日换个更远的行权价。")
    elif dte_now is not None and dte_now <= NEAR_EXPIRY_DTE and profit_captured is not None and profit_captured > 0:
        verdict = "临近到期,建议了结"
        reason = (f"只剩 {dte_now} 天到期,已经吃到 {profit_captured*100:.0f}% 的权利金了。"
                   "临到期 gamma 风险变大,剩下这点时间价值不值得再担风险,我会了结。")
    elif (profit_captured is not None and profit_captured >= LOCK_PROFIT_CAPTURED) or \
         (edge_of_holding is not None and edge_of_holding <= 0):
        verdict = "建议锁盈"
        if profit_captured is not None and profit_captured >= LOCK_PROFIT_CAPTURED:
            reason = (f"已经吃到 {profit_captured*100:.0f}% 的权利金,剩下的收益边际很薄了,"
                       "继续持有多担的风险不划算,我会平仓换下一笔。")
        else:
            reason = "按当前胜率和剩余风险算,继续持有到期的期望值已经不如现在平仓锁定的划算了。"
    elif edge_of_holding is not None and edge_of_holding > 0:
        verdict = "继续持有"
        reason = "胜率和已吃到的权利金都还在合理区间,统计上继续持有到期的期望值更高,我会拿着。"
    else:
        verdict = "浮亏观察,暂不到止损线"
        reason = "目前是浮亏,但短腿还没被击穿、delta 也没到危险区,我会先观察,不急着动。"

    return {
        "verdict": verdict, "reason": reason,
        "profit_captured": profit_captured,
        "edge_of_holding": edge_of_holding,
        "pop_entry": rec.get("entry_pop"),
        "pop_now": pop_now,
        "delta_now": None if delta_now is None else round(delta_now, 4),
    }


def _leg(chain, right, strike):
    for c in chain:
        if c["right"] == right and c["strike"] == strike:
            return c
    return None


def refresh():
    """重新估值全部 open 状态的观察记录。按 (symbol, expiry) 分组,减少行情调用。"""
    rows = _load()
    open_rows = [r for r in rows if r.get("status") == "open"]
    today = date.today()

    groups = {}
    for r in open_rows:
        groups.setdefault((r["symbol"], r["expiry"]), []).append(r)

    price_cache, chain_cache = {}, {}
    out = []
    for (symbol, expiry), recs in groups.items():
        try:
            dte_now = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
        except ValueError:
            dte_now = None

        if dte_now is not None and dte_now < 0:
            for r in recs:
                out.append({**r, "dte_now": dte_now,
                            "status_note": "已到期,请确认结果后删除观察"})
            continue

        try:
            if symbol not in price_cache:
                price_cache[symbol] = underlying_price(symbol)
            spot_now = price_cache[symbol]
            if (symbol, expiry) not in chain_cache:
                chain_cache[(symbol, expiry)] = normalize_chain(symbol, expiry)
            chain = chain_cache[(symbol, expiry)]
        except Exception as ex:
            for r in recs:
                out.append({**r, "error": str(ex)})
            continue

        for r in recs:
            try:
                short_leg = _leg(chain, r["right"], r["short_strike"])
                long_leg = _leg(chain, r["right"], r["long_strike"])
                if not short_leg or not long_leg:
                    out.append({**r, "error": "当前期权链里找不到这两条腿(可能已下市或行权价变了)"})
                    continue

                cost_to_close = round(short_leg["ask"] - long_leg["bid"], 4)
                pnl_dollars = round((r["entry_credit"] - cost_to_close) * 100, 2)
                pnl_pct = None if not r["entry_credit"] else round(
                    (r["entry_credit"] - cost_to_close) / r["entry_credit"], 4)

                T = max(dte_now, 0) / 365.0
                delta_now = short_leg.get("delta")
                if delta_now is None:
                    delta_now = _bs_delta(r["right"], spot_now, r["short_strike"], T,
                                           short_leg.get("iv"), 0.043)

                sugg = _suggest(r, cost_to_close, dte_now, delta_now, spot_now)

                out.append({
                    **r,
                    "spot_now": spot_now, "dte_now": dte_now,
                    "cost_to_close": cost_to_close,
                    "pnl_dollars": pnl_dollars, "pnl_pct": pnl_pct,
                    "suggestion": sugg,
                })
            except Exception as ex:
                out.append({**r, "error": str(ex)})

    closed = [r for r in rows if r.get("status") != "open"]
    return {"positions": out + closed, "count": len(out)}

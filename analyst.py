"""
analyst.py —— AI 期权分析(走公司网关调 Claude)
================================================
把某标的的持仓结构(Put/Call比、Max Pain、OI墓碑、IV偏斜)喂给 Claude,
让它用"期权小班长"的口吻输出一段简短分析:先给区间/方向判断,再落到一句可执行的卖方策略。

风格参考真实 KOL 帖子提炼:
- 大白话、笃定、短句;不堆术语。
- 先说股价大概在什么区间震荡 / 偏哪个方向。
- 用持仓结构支撑判断(支撑位=下方put墙,阻力位=上方call墙,Max Pain=磁吸位)。
- 警惕反常信号(远端大单可能是诱饵)。
- 收在一个具体动作:比如"当前位置更适合 sell call XXX"或"回调看到 XXX 再 sell put"。
- 结尾必带风险提示。不是投资建议。

凭证走环境变量 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN(公司网关,与 youtube-summarizer 同一套)。
"""
import os

import flow

MODEL = "claude-opus-4-8"

SYSTEM = """你是"期权小班长",一个用期权持仓结构判断股价走势的资深美股期权玩家。
你的读者是有经验的投资者,别科普基础概念。

说话风格:
- 中文,大白话,短句,笃定但不打包票。
- 先给结论(区间/方向),再用持仓数据支撑,最后落到一个具体的卖方策略动作。
- 用支撑位(下方 put 墙)、阻力位(上方 call 墙)、Max Pain(到期磁吸位)、Put/Call 比、IV 偏斜来讲理由。
- 如果数据有反常处(比如远端虚值堆了大单),点出来并提示可能是诱饵。

输出要求:
- 总共 4-6 句话,别写小标题,别分点列表,就是一段顺畅的口语分析。
- 必须包含:①股价大概区间或偏向 ②一个具体的卖方策略动作(sell put / sell call + 大概行权价)③一句风险提示。
- 只依据给你的持仓数据说话,不要编造没有的数字。数据是持仓存量(OI),不是当天新单,也看不出买开卖开,分析时注意这个局限。
- 结尾附一句:"仅个人研究,非投资建议。"
"""


def _fmt_signals(pos: dict) -> str:
    """把 positioning_for 的结构化结果拼成喂给模型的文字。"""
    s = pos.get("signals", {})
    price = pos.get("price")
    lines = [
        f"标的:{pos.get('symbol')}",
        f"现价:{price}",
        f"综合势能分:{pos.get('score')}(-100看空 ~ +100看多),程序判定:{pos.get('verdict')}",
        f"Put/Call 比(未平仓 OI 口径):{s.get('pcr_oi')}  (>1.2偏空,<0.7偏多)",
        f"Put/Call 比(当日成交量口径):{s.get('pcr_vol')}",
        f"call 总未平仓:{s.get('call_oi')}  put 总未平仓:{s.get('put_oi')}",
        f"Max Pain(最大痛苦点/到期磁吸位):{s.get('max_pain')}  "
        f"(相对现价{'上方' if (s.get('max_pain_gap') or 0) >= 0 else '下方'} "
        f"{abs((s.get('max_pain_gap') or 0))*100:.1f}%)",
        f"上方阻力位(call OI 墙):{s.get('resistance')}  该行权价未平仓:{s.get('resistance_oi')}",
        f"下方支撑位(put OI 墙):{s.get('support')}  该行权价未平仓:{s.get('support_oi')}",
        f"IV 偏斜(价外put IV − 价外call IV):{s.get('skew')}  "
        f"({'正=市场为下跌付更贵保险,偏防御/看空' if (s.get('skew') or 0) > 0 else '负=偏进攻/看多'})",
    ]
    exps = pos.get("expirations") or []
    if exps:
        dtes = [str(e.get("dte")) for e in exps if e.get("dte") is not None]
        if dtes:
            lines.append(f"分析覆盖的到期(距今天数):{', '.join(dtes)} 天")
    return "\n".join(lines)


def _client():
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("没配 ANTHROPIC_AUTH_TOKEN(公司网关令牌),AI 分析不可用。见 .env")
    import anthropic
    return anthropic.Anthropic(auth_token=token, base_url=base or None)


def analyze(symbol: str, params: dict = None) -> dict:
    """拉持仓结构 → 交给 Claude 写一段小班长风格分析。返回 {symbol, price, verdict, score, signals, analysis}。"""
    pos = flow.positioning_for(symbol, params)
    if pos.get("error"):
        return {"symbol": symbol.upper(), "error": pos["error"]}

    prompt = (
        "下面是这个标的近端到期的期权持仓结构数据,请按你的风格给一段简短分析:\n\n"
        + _fmt_signals(pos)
    )
    try:
        client = _client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as ex:
        return {**pos, "error": f"AI 分析调用失败:{ex}"}

    return {
        "symbol": pos["symbol"],
        "price": pos["price"],
        "verdict": pos.get("verdict"),
        "score": pos.get("score"),
        "signals": pos.get("signals"),
        "analysis": text,
    }

"""
app.py —— 垂直价差机会发现网站 后端
================================================
启动: .venv\\Scripts\\python.exe app.py   然后浏览器开 http://localhost:3100

接口:
  GET /api/status                权限自检:美股期权行情是否已开通
  GET /api/watchlist             返回精选池
  GET /api/scan                  扫全池,全局排序
  GET /api/scan?ticker=NVDA      只扫单个标的(含不在池里的任意代码)
  可选参数(覆盖默认): min_dte,max_dte,min_ror,min_pop,min_oi,short_delta_min,short_delta_max
内存缓存 TTL 5 分钟,避免重复扫描狂打老虎 API;响应带 asOf 时间戳。
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections import defaultdict, deque

# Windows 控制台默认 GBK,打印中文/emoji 会崩;强制 utf-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory

import screener
import tiger

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 3100
CACHE_TTL = 300  # 秒
MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]  # 七大科技股

LOGIN_MAX_ATTEMPTS = 5     # 窗口期内允许的失败次数
LOGIN_WINDOW_SEC = 600     # 失败计数窗口:10 分钟
LOGIN_LOCKOUT_SEC = 600    # 超限后锁定时长:10 分钟


def _load_password():
    """访问密码:优先环境变量 SCANNER_PASSWORD,否则读 access_password.txt,否则默认。
    公网分享给熟人前,请改掉默认密码。"""
    pw = os.environ.get("SCANNER_PASSWORD")
    if pw:
        return pw.strip()
    p = os.path.join(HERE, "access_password.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return "tiger2026"  # ⚠️ 默认密码,公网分享前务必改


def _load_secret():
    """cookie 签名密钥,独立于访问密码持久化存储,避免 cookie 泄露=密码泄露。"""
    p = os.path.join(HERE, ".secret_key")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            val = f.read().strip()
            if val:
                return val
    val = secrets.token_hex(32)
    with open(p, "w", encoding="utf-8") as f:
        f.write(val)
    return val


PASSWORD = _load_password()
SECRET = _load_secret()
_login_fail_log = defaultdict(deque)  # ip -> deque[失败时间戳]


def _auth_token():
    """基于密码 + 独立密钥派生的 cookie 值,而非明文密码本身。"""
    return hmac.new(SECRET.encode(), PASSWORD.encode(), hashlib.sha256).hexdigest()


def _client_ip():
    return request.headers.get("Cf-Connecting-Ip") or request.remote_addr or "unknown"


def _login_locked(ip):
    now = time.time()
    q = _login_fail_log[ip]
    while q and now - q[0] > LOGIN_WINDOW_SEC:
        q.popleft()
    if len(q) < LOGIN_MAX_ATTEMPTS:
        return False
    return now - q[-1] < LOGIN_LOCKOUT_SEC


def _login_record_failure(ip):
    _login_fail_log[ip].append(time.time())

LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>登录</title>
<style>body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;display:flex;min-height:90vh;
align-items:center;justify-content:center;background:#fafafa}}form{{background:#fff;border:1px solid #e5e7eb;
border-radius:14px;padding:28px 26px;width:280px;text-align:center}}h2{{margin:0 0 4px;font-size:18px}}
p{{color:#888;font-size:13px;margin:0 0 16px}}input{{width:100%;box-sizing:border-box;padding:10px 12px;
font-size:15px;border:1px solid #ccc;border-radius:8px;margin-bottom:12px}}button{{width:100%;padding:10px;
font-size:15px;font-weight:600;background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer}}
.err{{color:#dc2626;font-size:13px;margin-bottom:10px}}</style></head><body>
<form method="POST" action="/login"><h2>📐 垂直价差扫描</h2><p>请输入访问密码</p>
{err}<input type="password" name="password" placeholder="访问密码" autofocus>
<button>进入</button></form></body></html>"""

app = Flask(__name__, static_folder=None)
_cache = {}  # key -> (ts, data)


def _is_local_direct():
    """本机浏览器直连(非隧道):remote_addr 是回环地址,且没有任何代理转发头。
    Cloudflare 隧道会带 X-Forwarded-For / Cf-Connecting-Ip,据此区分外网访问。"""
    if request.headers.get("X-Forwarded-For") or request.headers.get("Cf-Connecting-Ip"):
        return False
    return request.remote_addr in ("127.0.0.1", "::1")


@app.before_request
def _auth_gate():
    # 放行登录路由本身
    if request.path == "/login":
        return None
    # 本机直连免密
    if _is_local_direct():
        return None
    # cookie 校验(定长比较防时序攻击)
    tok = request.cookies.get("sc_auth", "")
    if hmac.compare_digest(tok, _auth_token()):
        return None
    # 未授权:接口返 401,页面返登录表单
    if request.path.startswith("/api"):
        return jsonify({"error": "未授权,请先登录"}), 401
    return LOGIN_HTML.format(err=""), 401


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = _client_ip()
    if request.method == "POST":
        if _login_locked(ip):
            err = '<div class="err">尝试次数过多,请 10 分钟后再试</div>'
            return LOGIN_HTML.format(err=err), 429
        if hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            resp = make_response(redirect("/"))
            # 30 天免登;samesite=Lax 便于分享链接打开;cookie 值为派生 token,非明文密码
            resp.set_cookie("sc_auth", _auth_token(), max_age=30 * 86400, httponly=True, samesite="Lax", secure=True)
            return resp
        _login_record_failure(ip)
        err = '<div class="err">密码错误</div>' if request.method == "POST" else ""
        return LOGIN_HTML.format(err=err), 401
    return LOGIN_HTML.format(err=""), 200


def _load_watchlist():
    with open(os.path.join(HERE, "watchlist.json"), encoding="utf-8") as f:
        wl = json.load(f)
    syms = []
    for g in wl.get("groups", {}).values():
        syms.extend(g)
    # 去重保序
    seen, out = set(), []
    for s in syms:
        u = s.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out, wl.get("groups", {})


def _params_from_request():
    p = {}
    for k in ("min_dte", "max_dte", "min_oi", "max_legs_away", "top_n"):
        if k in request.args:
            p[k] = int(float(request.args[k]))
    for k in ("min_ror", "min_pop", "short_delta_min", "short_delta_max", "max_spread_pct"):
        if k in request.args:
            p[k] = float(request.args[k])
    return p


def _cache_get(key):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key, data):
    _cache[key] = (time.time(), data)
    return data


# ---------------------------------------------------------------- 路由
@app.get("/api/status")
def status():
    perms = tiger.quote_permissions()
    names = [p.get("name") for p in perms if isinstance(p, dict)]
    return jsonify({
        "permissions": perms,
        "us_option_ok": "usOptionQuote" in names,
        "us_stock_ok": "usStockQuote" in names,
        "hint": "去 Tiger Trade App → 我的 → 行情权限 → OpenAPI权限 购买 usStockQuote + usOptionQuote",
    })


@app.get("/api/watchlist")
def watchlist():
    syms, groups = _load_watchlist()
    return jsonify({"symbols": syms, "groups": groups})


@app.get("/api/tech-cadence")
def tech_cadence():
    """七大科技股的到期节奏(按到期频率降序),挑高频到期(每周一三五)标的用。"""
    key = "TECH_CADENCE"
    cached = _cache_get(key)
    if cached and request.args.get("force") != "1":
        return jsonify({**cached, "cached": True})
    out = []
    for sym in MAG7:
        try:
            out.append(screener.expiry_cadence(sym))
        except Exception as ex:
            out.append({"symbol": sym, "label": "出错", "error": str(ex), "count": 0, "expiries": []})
    out.sort(key=lambda x: x.get("count", 0), reverse=True)
    data = {"stocks": out, "asOf": time.strftime("%Y-%m-%d %H:%M:%S"), "cached": False}
    return jsonify(_cache_put(key, data))


@app.get("/api/scan")
def scan():
    p = _params_from_request()
    ticker = (request.args.get("ticker") or "").upper().strip()
    force = request.args.get("force") == "1"
    key = json.dumps({"t": ticker or "ALL", "p": p}, sort_keys=True)

    if not force:
        cached = _cache_get(key)
        if cached:
            return jsonify({**cached, "cached": True})

    t0 = time.time()
    if ticker:
        res = screener.scan_symbol(ticker, p)
    else:
        syms, _ = _load_watchlist()
        res = screener.scan_many(syms, p)
    res["asOf"] = time.strftime("%Y-%m-%d %H:%M:%S")
    res["elapsed"] = round(time.time() - t0, 1)
    res["cached"] = False
    return jsonify(_cache_put(key, res))


# 静态托管
@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "public"), "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory(os.path.join(HERE, "public"), path)


if __name__ == "__main__":
    print(f"\n✅ 价差扫描器已启动: http://localhost:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, threaded=True)

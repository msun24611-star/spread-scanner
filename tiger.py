"""
tiger.py —— 老虎 Open API 只读行情客户端封装
================================================
照搬 tiger-mcp/server.py 的初始化写法,复用同一套凭证(private_key.pem + TIGER_ID)。
进程内长驻一个 QuoteClient 单例,供 Flask 各请求复用,避免每次重新签名认证。

需要的行情权限(去 Tiger Trade App → 我的 → 行情权限 → OpenAPI权限 购买):
  - usStockQuote   美股 LV1 行情(拿标的现价)
  - usOptionQuote  美股期权行情 OPRA(拿期权链 + greeks)
用 quote_permissions() 可查当前账户到底有哪些权限。
"""
import os
import threading

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.common.util.signature_utils import read_private_key
from tigeropen.common.consts import Language
from tigeropen.quote.quote_client import QuoteClient

# 凭证一律从环境变量读,不硬编码(本仓库为公开仓库)。
# 本地运行前请设置:TIGER_PRIVATE_KEY_PATH / TIGER_ID / TIGER_ACCOUNT,详见 .env.example。
_client = None
_lock = threading.Lock()


def _config() -> TigerOpenClientConfig:
    key_path = os.environ.get("TIGER_PRIVATE_KEY_PATH", "")
    tiger_id = os.environ.get("TIGER_ID", "")
    account = os.environ.get("TIGER_ACCOUNT", "")
    if not tiger_id or not account:
        raise RuntimeError("请先设置环境变量 TIGER_ID 和 TIGER_ACCOUNT(参见 .env.example)")
    if not key_path or not os.path.exists(key_path):
        raise RuntimeError(f"找不到私钥文件,请设置 TIGER_PRIVATE_KEY_PATH。当前值: {key_path!r}")
    cfg = TigerOpenClientConfig(sandbox_debug=False)
    cfg.private_key = read_private_key(key_path)
    cfg.tiger_id = str(tiger_id)
    cfg.account = str(account)
    cfg.language = Language.zh_CN
    return cfg


def quote() -> QuoteClient:
    """惰性单例。第一次调用时建 client,之后复用。线程安全。"""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = QuoteClient(_config())
    return _client


def quote_permissions() -> list:
    """查当前行情权限名单,如 [{'name':'usOptionQuote','expire_at':...}, ...]。
    用来判断美股期权权限是否已开通(找 name == 'usOptionQuote')。"""
    try:
        return quote().get_quote_permission()
    except Exception as e:
        return [{"error": str(e)}]


def has_us_option_permission() -> bool:
    perms = quote_permissions()
    names = {p.get("name") for p in perms if isinstance(p, dict)}
    return "usOptionQuote" in names

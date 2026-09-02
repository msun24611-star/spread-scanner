"""
start.py —— 读 .env、校验凭证与访问密码,然后拉起 app.py。

由 start.bat 调用,也可以直接跑:.venv\\Scripts\\python.exe start.py
校验放在这里而不是 .bat 里,是因为 cmd.exe 按旧代码页解析批处理,
中文写进 .bat 会把解析器搞坏(踩过一次)。
"""
import os
import runpy
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
REQUIRED = ("TIGER_ID", "TIGER_ACCOUNT", "TIGER_PRIVATE_KEY_PATH")


def die(title, *lines):
    print()
    print("  [X] " + title)
    for ln in lines:
        print("      " + ln)
    print()
    print("  " + "-" * 56)
    sys.exit(1)


def load_dotenv(path):
    """极简 .env 解析:KEY=VALUE,# 开头为注释,值两侧的引号会去掉。
    已经存在的真实环境变量优先,不会被 .env 覆盖。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=enc) as f:
                raw_lines = f.readlines()
            break
        except UnicodeDecodeError:
            continue
    else:
        die(".env 编码无法识别", "请用 UTF-8 或 GBK 保存 .env。")

    loaded = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, val)
        loaded.append(key)
    return loaded


def mask(val, head=0, tail=0):
    """脱敏回显,永远不把完整凭证打到控制台/日志里。"""
    if not val:
        return ""
    if len(val) <= head + tail:
        return "*" * len(val)
    return (val[:head] if head else "") + "***" + (val[-tail:] if tail else "")


def main():
    print()
    print("  [ 垂直价差扫描器 ]")

    # ---------- 1. .env ----------
    if not os.path.exists(ENV_PATH):
        die(
            "找不到 .env",
            "先复制模板,再填入你自己的老虎凭证:",
            "",
            "    copy .env.example .env",
            "    notepad .env",
        )
    load_dotenv(ENV_PATH)

    # ---------- 2. 必填项 ----------
    missing = [k for k in REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        die(".env 里缺这几项:" + "、".join(missing))

    # ---------- 3. 占位符没改 ----------
    placeholders = [k for k in REQUIRED
                    if "你的_" in os.environ[k] or "path\\to\\your" in os.environ[k]]
    if placeholders:
        die(
            "这几项还是 .env.example 里的占位符,没改过:" + "、".join(placeholders),
            "填成你自己的值再启动。",
        )

    # ---------- 4. 私钥文件 ----------
    key_path = os.environ["TIGER_PRIVATE_KEY_PATH"]
    # 用 isfile 而不是 exists:半截路径(比如 "C:")在 exists 下是 True,会放行垃圾配置。
    if not os.path.isfile(key_path):
        die(
            "私钥文件不存在:" + key_path,
            "去老虎开发者后台生成 RSA 私钥,把 .pem 的绝对路径填进 .env。",
        )

    # ---------- 5. 访问密码(app.py 已改为失败关闭,没有默认值) ----------
    pw = os.environ.get("SCANNER_PASSWORD", "").strip()
    pw_file = os.path.join(HERE, "access_password.txt")
    if not pw:
        ok = False
        if os.path.exists(pw_file):
            with open(pw_file, encoding="utf-8") as f:
                ok = bool(f.read().strip())
        if not ok:
            die(
                "没有访问密码",
                ".env 里没写 SCANNER_PASSWORD,access_password.txt 也不存在或是空的。",
                "任选其一设置好再启动 —— 这个服务会走公网隧道,没密码等于裸奔。",
            )
        pw_src = "access_password.txt"
    else:
        pw_src = ".env 的 SCANNER_PASSWORD"

    # ---------- 6. 依赖 ----------
    try:
        import flask  # noqa: F401
        import tigeropen  # noqa: F401
    except ImportError as ex:
        die(
            "依赖没装齐:" + str(ex),
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt",
        )

    # ---------- 7. 启动 ----------
    print()
    print("  TIGER_ID       " + mask(os.environ["TIGER_ID"], head=3))
    print("  TIGER_ACCOUNT  " + mask(os.environ["TIGER_ACCOUNT"], tail=4))
    print("  私钥           " + key_path)
    print("  访问密码       来自 " + pw_src)
    print("  访问地址       http://localhost:3100")
    print()
    print("  " + "-" * 56)

    sys.argv = [os.path.join(HERE, "app.py")]
    runpy.run_path(os.path.join(HERE, "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()

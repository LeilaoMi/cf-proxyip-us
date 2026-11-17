"""auto_update.py / audit.py / sync_kv.py 共用的小工具，避免多份实现慢慢走样。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import time
import tomllib
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parent.parent
LIST_DOMAIN = os.environ.get("PROXYIP_LIST_DOMAIN", "https://list.leilaomi.cc.cd").rstrip("/")
PROXY_DOMAIN = os.environ.get("PROXYIP_RECORD_NAME", "proxyip.leilaomi.cc.cd")
USER_AGENT = "Mozilla/5.0 ProxyIPAutomation"


def wrangler_cmd() -> str:
    """优先用仓库 node_modules/.bin 里按 package-lock 锁定的 wrangler。
    shutil.which 在 Windows 上会按 PATHEXT 解析出 wrangler.cmd，subprocess 直接传 "wrangler" 解析不到。"""
    local_bin = ROOT / "node_modules" / ".bin"
    return shutil.which("wrangler", path=str(local_bin)) or shutil.which("wrangler") or "wrangler"


def kv_namespace_id(toml_path: Path | None = None) -> str:
    """按 TOML 结构读 [[kv_namespaces]] 的 id，不用正则，免得匹配到 account_id 等别的 id。"""
    data = tomllib.loads((toml_path or ROOT / "wrangler.toml").read_text(encoding="utf-8"))
    for ns in data.get("kv_namespaces") or []:
        if ns.get("binding") == "PROXYIP_KV" and ns.get("id"):
            return str(ns["id"])
    raise RuntimeError("Cannot find PROXYIP_KV namespace id in wrangler.toml")


def hmac_token() -> str:
    """当天的 HMAC token，与 Worker 校验逻辑一致；没有 secret 时退回首页 cookie 换 token。"""
    secret = os.environ.get("PROXYIP_HMAC_SECRET", "")
    if secret:
        date_str = time.strftime("%Y%m%d", time.gmtime())
        return f"{date_str}-{hmac.new(secret.encode(), date_str.encode(), hashlib.sha256).hexdigest()}"

    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    opener.open(Request(f"{LIST_DOMAIN}/", headers={"User-Agent": USER_AGENT}), timeout=30).read()
    with opener.open(Request(f"{LIST_DOMAIN}/token", headers={"User-Agent": USER_AGENT}), timeout=30) as res:
        data = json.loads(res.read().decode("utf-8", "ignore"))
    token = data.get("token")
    if not token:
        raise SystemExit("Cannot obtain HMAC token from /token")
    return token


def fetch(url: str, token: str | None = None, timeout: int = 30) -> tuple[int, str]:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", "ignore")
    except Exception as exc:
        return getattr(exc, "code", 0) or 0, str(exc)

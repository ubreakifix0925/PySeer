"""淘米 (Taomee / 61.com) 帐号认证 -> 获取登录 session.

该模块复刻 seerNew 的 get_session: 向淘米帐号中心发起 JSONP 认证请求,
从返回的 JSONP 里取出 data.session, 供后续登录封包使用.
"""

import json
import random
import re
import time
from urllib.request import Request, urlopen
from gzip import decompress as gzip_decompress
from zlib import decompress as zlib_decompress

from .algorithm import md5

# 淘米帐号中心认证入口 (可被命令行 --auth-url 覆盖)
ACCOUNT_AUTH_URL = "https://account-co.61.com/index.php?r=userIdentity/authenticate"


def current_timestamp_ms() -> int:
    return int(round(time.time() * 1000))


def jquery_mock_callback() -> str:
    """伪造一个形如 jQuery1_7_2_1234567890_1234 的 JSONP 回调名."""
    return "jQuery" + ("1.7.2" + str(random.random())).replace(".", "") + "_" + str(current_timestamp_ms() - 1000)


def _http_get(url: str, timeout: float = 15):
    """发 GET 请求并返回解码后的文本 (兼容 gzip / deflate)."""
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.61.com/",
    })
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip_decompress(raw)
        elif enc == "deflate":
            try:
                raw = zlib_decompress(raw)
            except Exception:
                raw = zlib_decompress(raw, -zlib.MAX_WBITS)
        for charset in ("utf-8", "gbk", "gb2312"):
            try:
                return raw.decode(charset)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")


def _extract_jsonp(text: str) -> dict:
    """从 JSONP 响应里抽出真正的 JSON 对象."""
    m = re.search(r"\(\s*(\{.*\})\s*\)\s*;?\s*$", text, re.S)
    if not m:
        # 有些接口直接返回 JSON
        m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("无法从响应中提取 JSON: %r" % text[:200])
    return json.loads(m.group(1))


def get_session(account: str, password: str, auth_url: str = ACCOUNT_AUTH_URL, timeout: float = 15) -> str:
    """用 米米号(account) + 明文密码(password) 换取 session.

    password 会被 MD5 后再提交, 与官方 flash 客户端一致.
    返回淘米返回的 data.session 字符串.
    """
    password = md5(password)
    callback = jquery_mock_callback()
    ts = current_timestamp_ms()
    url = (auth_url
           + "&callback=" + callback
           + "&account=" + account
           + "&rememberAcc=false"
           + "&passwd=" + password
           + "&rememberPwd=true"
           + "&vericode="
           + "&game=02"
           + "&tad=none"
           + "&_=" + str(ts)
           + "/authenticate/login&gid=206&tad=none")

    text = _http_get(url, timeout)
    data = _extract_jsonp(text)

    # 淘米返回结构: {"data": {"session": "...", ...}, "msg": ..., "status": ...}
    if "data" not in data or not data.get("data"):
        raise RuntimeError(f"认证失败, 服务器返回: {data}")
    session_value = data["data"].get("session")
    if not session_value:
        raise RuntimeError(f"认证响应不含 session: {data}")
    return session_value

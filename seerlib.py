"""赛尔号脚本库: 基于 seer-login-test 后端 (webui) 的 HTTP API.

后端启动并登录游戏账号后, 脚本通过本库即可让后端发/收包并取值.

快速示例::

    from seerlib import Seer
    s = Seer()                              # 运行时自动指向已登录后端 (无需硬编码地址)
    s.send(43706)                          # 无参发包(刷背包)
    pkt = s.recv(2301, [3266, 0, 0, 0])    # 发包并等该命令的 RECV, 返回 Packet
    v = s.get_value(pkt, 0)                # 取包体第 0 个 int32
    print(pkt.ints, v)

后端地址自动发现: 见 discover_backend() —— 显式参数 > 环境变量 ``SEER_BACKEND`` >
后端启动时写入的 ``webui_addr.json`` > 逐端口探测附近仍在线的后端 > 兜底 ``http://127.0.0.1:8680``.
(当 webui_addr.json 指向的后端已下线时, 会自动逐端口探测并回退到仍在线的实例.)

三大函数:
    send(cmd, params)      -> 发送 SEND 包 (不等待响应), 返回后端应答 dict
    recv(cmd, params)      -> 发送 SEND 包并等待 RECV, 返回 Packet(body/ints/raw)
    get_value(body, index) -> 从包体取第 index 个值 (int32 大端)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


class SeerError(Exception):
    """库调用失败 (未登录 / 参数错误 / 等待响应超时 / 取值越界等)."""


def _hex(hexstr: str) -> bytes:
    return bytes.fromhex("".join(c for c in hexstr if c in "0123456789abcdefABCDEF"))


# ---- 后端地址自动发现 ----

# 未指定/未发现时的兜底默认地址 (webui 常用端口)
DEFAULT_BASE = "http://127.0.0.1:8680"

# 后端(webui.py)启动时会把"实际监听地址"写入该文件, 供脚本运行时定位.
# 位置固定在本文件同目录, 与 webui.py 的 _ADDR_FILE 一致.
_ADDR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui_addr.json")

# 逐端口探测的端口范围: webui 默认 8680; --port 0 / 换端口时可能落到这附近.
_PROBE_PORTS = list(range(8680, 8700))

# 地址探测超时(秒): 只用于"判断某地址是否为存活后端", 取小值快速跳过离线端口.
_PROBE_TIMEOUT = 1.0


def _normalize_base(base) -> str:
    return str(base).rstrip("/")


def _probe_alive(base, timeout=_PROBE_TIMEOUT) -> bool:
    """判断 base 是否为存活的 seer 后端: GET /api/status 返回含 'status' 的 JSON.

    用 /api/status 而非仅端口可达, 可避免误把其它 http 服务当作后端.
    """
    try:
        req = urllib.request.Request(base + "/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return isinstance(data, dict) and "status" in data
    except Exception:
        return False


def _candidate_bases():
    """生成有序、去重的候选后端地址列表 (用于逐端口探测回退).

    顺序: webui_addr.json 里的地址 → 默认 >> 附近端口逐一扫.
    """
    bases = []

    def add(u):
        u = _normalize_base(u)
        if u not in bases:
            bases.append(u)

    # 1. 后端启动时写入的 webui_addr.json (最高优先)
    try:
        if os.path.exists(_ADDR_FILE):
            with open(_ADDR_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            u = d.get("url") or d.get("base") if isinstance(d, dict) else d
            if u:
                add(u)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    # 2. 默认端口
    add(DEFAULT_BASE)
    # 3. 逐端口扫描附近端口 (覆盖 --port 0 / 换端口后仍在线的实例)
    for p in _PROBE_PORTS:
        add(f"http://127.0.0.1:{p}")
    return bases


def discover_backend(explicit=None, probe=True, timeout=_PROBE_TIMEOUT) -> str:
    """运行时自动定位已登录的后端地址, 按优先级:

    1. 显式传入的 base 参数   (脚本里显式指定, 优先级最高, 不做探测)
    2. 环境变量 ``SEER_BACKEND`` (显式覆盖, 不做探测)
    3. ``webui_addr.json`` 里的地址; 若该后端已下线, 自动逐端口探测回退到仍在线的实例
    4. 兜底默认 ``http://127.0.0.1:8680``

    因此脚本可以省略参数写成 ``s = Seer()``, 无需在代码里硬编码后端地址.
    返回的是去掉尾部 ``/`` 的 base 字符串.
    """
    if explicit:
        return _normalize_base(explicit)
    env = os.environ.get("SEER_BACKEND")
    if env:
        return _normalize_base(env)
    candidates = _candidate_bases()
    if probe:
        for base in candidates:
            if _probe_alive(base, timeout):
                return base
    # 无在线实例时返回首个候选(最可能是正确地址), 交由调用方在使用时报错
    return candidates[0]


class Packet:
    """一条 RECV 包体: body(hex) + ints(十进制列表) + raw(bytes)."""

    def __init__(self, body: str, ints: list, cmd: int):
        self.body = body          # 完整包体(hex 字符串)
        self.ints = list(ints)    # 按 4 字节大端 int32 拆出的十进制列表
        self.cmd = int(cmd)
        self.raw = _hex(body) if body else b""

    def __getitem__(self, i: int) -> int:
        return self.ints[i]

    def __len__(self) -> int:
        return len(self.ints)

    def get(self, i: int) -> int:
        return self.ints[i]

    def __repr__(self) -> str:
        return f"<SeerPacket cmd={self.cmd} len={len(self.ints)} body={self.body[:32]}...>"


class Seer:
    """绑定一个已登录的后端地址, 提供发/收/取包功能.

    地址可省: 默认按 ``discover_backend()`` 自动定位 (环境变量 ``SEER_BACKEND`` ->
    ``webui_addr.json`` -> 逐端口探测附近仍在线的后端 -> 兜底 ``http://127.0.0.1:8680``),
    因此可直接 ``s = Seer()``.
    """

    def __init__(self, base=None, timeout: float = 30.0, probe: bool = True,
                 probe_timeout: float = None):
        self.base = discover_backend(base,
                                     probe=probe,
                                     timeout=probe_timeout or _PROBE_TIMEOUT)
        self.timeout = timeout

    # ---------- 底层 HTTP ----------
    def _post(self, path: str, data: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
                j = json.loads(body)
            except Exception:
                raise SeerError(f"HTTP {e.code}: {e.reason}") from e
            if isinstance(j, dict) and not j.get("ok", True):
                raise SeerError(j.get("error", f"HTTP {e.code}"))
            raise SeerError(f"HTTP {e.code}: {body[:200]}") from e
        except (urllib.error.URLError, ConnectionError) as e:
            raise SeerError(f"连接失败: {getattr(e, 'reason', e)}") from e

    # ---------- 参数列表 -> 包体 spec (对齐 seer.body.pack_body) ----------
    @staticmethod
    def _spec(params) -> str:
        """把 参数列表 转成后端 /api/send 认识的"包体spec"字符串.

        数字 -> int32 大端; str 含 s:/b:/h: 前缀则原样; bytes -> h:hex;
        None -> 跳过.
        """
        if params is None:
            return ""
        if isinstance(params, str):
            return params
        if isinstance(params, (list, tuple)):
            parts = []
            for p in params:
                if p is None:
                    continue
                if isinstance(p, str) and re.match(r"^[sbh]:", p):
                    parts.append(str(p))
                elif isinstance(p, bytes):
                    parts.append("h:" + p.hex())
                else:
                    parts.append(str(int(p)))
            return ",".join(parts)
        return str(params)

    # ---------- 三大函数 ----------
    def send(self, cmd, params=None) -> dict:
        """发送 SEND 包(不等待响应). cmd 可为 int 或命令名(如 'ENTER_MAP'). 返回后端应答 dict."""
        j = self._post("/api/send", {
            "cmd": str(cmd), "body": self._spec(params), "encode": "pack"})
        if not j.get("ok"):
            raise SeerError(j.get("error", "发送失败"))
        return j

    def recv(self, cmd, params=None, timeout: float = 8.0) -> Packet:
        """发送 SEND 包并等待该命令的 RECV 应答, 返回 Packet(body/ints/raw)."""
        j = self._post("/api/send-recv", {
            "cmd": str(cmd), "body": self._spec(params), "timeout": timeout})
        if not j.get("ok"):
            raise SeerError(j.get("error", "recv 失败") or "等待响应失败")
        return Packet(j.get("body", ""), j.get("ints", []), j.get("cmd", cmd))

    def get_value(self, body, index: int) -> int:
        """从包体取第 index 个值(int32 大端). body 可为 Packet/hex str/bytes."""
        return get_value(body, index)


# ---------- 取值函数 (模块级) ----------
def get_value(body, index: int) -> int:
    """从包体取第 index 个值(int32 大端). body 可为 Packet / hex str / bytes."""
    if isinstance(body, Packet):
        return body.ints[index]
    if isinstance(body, bytes):
        b = body
    else:
        b = _hex(str(body))
    i = index * 4
    if i + 4 > len(b):
        raise SeerError(f"取值索引 {index} 越界 (包体共 {len(b)//4} 个 int32)")
    return int.from_bytes(b[i:i + 4], "big", signed=False)


if __name__ == "__main__":
    # 简单自检: 需要后端已登录
    s = Seer()          # 自动发现后端地址 (含下线后逐端口探测回退)
    print("后端地址:", s.base)
    print("刷新背包:", s.send(43706))
    pkt = s.recv(43706)
    print("43706 RECV 包体:", pkt.body)
    print("int32 序列:", pkt.ints[:10])
    print("取第0个值:", s.get_value(pkt, 0))

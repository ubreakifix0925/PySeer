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

高阶: set_bag(ids) <-> 把背包全部切换为指定物种 id 列表 (物理重排, 会发真实游戏命令).
      find_pet(ids) <-> 查找指定物种 id 是否存在, 及所在位置(背包1/背包2/仓库/**精英背包**).
    get_value(body, index) -> 从包体取第 index 个值 (int32 大端)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


# 源码目录 (app/) 与项目根目录 (其上一级); 运行时数据(petbook.json/webui_addr.json)在 data/ 下
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_SRC_DIR)
_DATA_DIR = os.path.join(_PROJ, "data")


class SeerError(Exception):
    """库调用失败 (未登录 / 参数错误 / 等待响应超时 / 取值越界等)."""


def _hex(hexstr: str) -> bytes:
    return bytes.fromhex("".join(c for c in hexstr if c in "0123456789abcdefABCDEF"))


# ---- 物种 id -> 中文名 (petbook.json, 懒加载) ----
_PETBOOK = None


def _pet_name(sid) -> str:
    """按物种 id 查中文名 (petbook.json 的 {"id":"名字"}); 查不到返回 '未知'."""
    global _PETBOOK
    if _PETBOOK is None:
        _PETBOOK = {}
        try:
            p = os.path.join(_DATA_DIR, "petbook.json")
            with open(p, "r", encoding="utf-8") as f:
                _PETBOOK = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return _PETBOOK.get(str(sid)) or "未知"


# ---- 后端地址自动发现 ----

# 未指定/未发现时的兜底默认地址 (webui 常用端口)
DEFAULT_BASE = "http://127.0.0.1:8680"

# 后端(webui.py)启动时会把"实际监听地址"写入该文件, 供脚本运行时定位.
# 位置固定在本文件同目录, 与 webui.py 的 _ADDR_FILE 一致.
_ADDR_FILE = os.path.join(_DATA_DIR, "webui_addr.json")

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

    # ---------- 换背包 (物种 id -> 物理重排 12 格) ----------
    def _get_json(self, path: str) -> dict:
        """GET 请求 -> JSON dict (后端 /api/bag、/api/storage 等)."""
        req = urllib.request.Request(self.base + path, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _wait_bag(self, timeout: float = 10.0):
        """发送 43706 后轮询 /api/bag, 直到后台按 43706 应答解析出背包数据 (fetched)."""
        import time as _t
        start = _t.time()
        while _t.time() - start < timeout:
            try:
                j = self._get_json("/api/bag")
            except Exception as e:
                raise SeerError(f"读取背包失败: {e}")
            if j.get("fetched"):
                return j
            _t.sleep(0.3)
        raise SeerError("等待背包数据超时(请确认后端已登录)")

    def _ensure_storage(self, timeout: float = 20.0):
        """触发后端拉仓库并等数据稳定, 返回 [(catchTime, id), ...]."""
        import time as _t
        try:
            self._post("/api/storage/fetch", {})
        except Exception:
            pass
        start = _t.time()
        last_n, stable = -1, 0
        while _t.time() - start < timeout:
            try:
                j = self._get_json("/api/storage")
            except Exception as e:
                raise SeerError(f"读取仓库失败: {e}")
            if j.get("fetched"):
                pets = j.get("pets", [])
                n = len(pets)
                if n == last_n:
                    stable += 1
                    if stable >= 2:   # 分页到齐(数量不再变)即返回
                        return [(p.get("catchTime"), p.get("id"))
                                for p in pets if p.get("catchTime")]
                else:
                    stable, last_n = 0, n
            _t.sleep(0.4)
        raise SeerError("等待仓库数据超时(请确认后端已登录)")

    @staticmethod
    def _slot_catch(bag) -> list:
        """把 /api/bag 的 first/second 转成 12 格 catchTime 列表 (空格=0)."""
        slots = [0] * 12
        for i, p in enumerate(bag.get("first", [])):
            if i < 6:
                slots[i] = p.get("catchTime") or 0
        for i, p in enumerate(bag.get("second", [])):
            if i < 6:
                slots[6 + i] = p.get("catchTime") or 0
        return slots

    def set_bag(self, ids) -> dict:
        """把背包**全部**切换为指定**物种 id** 列表, 顺序即 12 格位 (前6=出战, 后6=待命).

        ids: 物种id列表 (长度<=12). 例: ``s.set_bag([466, 467, 468])``.

        流程: 读当前背包+仓库+**精英背包**(先校验目标物种都存在, 避免改动一半失败) -> 全部存仓库(2304)
        -> 按列表从仓库/精英取回(前6进第一背包, 后6进第二背包) -> 设首发(2308) -> 摆正顺序(41462 交换)。

        约定: 同一物种取池里(背包/仓库/精英)第一个未用之的; 若某些物种在背包/仓库/精英背包
        都**检测不到**, 则输出 ``找不到指定的精灵：名称[ID=xx]，名称[ID=yy]...！``
        (列出全部缺失的精灵) 并中止脚本运行(``SystemExit(1)``, 此时尚未改动背包);
        列表不足12时, 多余的格位留空(超额背包精灵已存入仓库)。
        精英宠物同样经 2304 取出(与仓库一致, 对齐 WebUI 精英仓库的拖拽交互)。

        注意: 该函数会发真实游戏命令(2304/2308/41462), 请用安全/可恢复的列表测试。
        """
        import time as _t
        if not isinstance(ids, (list, tuple)):
            raise SeerError("ids 需为列表")
        ids = [int(x) for x in ids]
        if not ids:
            raise SeerError("ids 不能为空")
        if len(ids) > 12:
            raise SeerError("id 列表最多 12 个")

        # 0. 刷新并读取当前背包 (只读, 不改动)
        self.send(43706)
        bag = self._wait_bag()
        first = bag.get("first", [])
        second = bag.get("second", [])

        # 1. 先读仓库+精英背包并**校验**目标物种都存在 (失败即返回, 背包未被改动)
        storage = self._ensure_storage()
        exe = []
        try:
            exe = self._ensure_exe()   # 精英背包(2361); 拉不到则只用背包+仓库
        except SeerError:
            pass
        by_species = {}
        for ct, pid in storage:
            by_species.setdefault(pid, []).append(ct)
        for ct, pid in exe:
            by_species.setdefault(pid, []).append(ct)
        for p in first:
            if p.get("id"):
                by_species.setdefault(p["id"], []).append(p["catchTime"])
        for p in second:
            if p.get("id"):
                by_species.setdefault(p["id"], []).append(p["catchTime"])
        chosen = []   # [(catchTime, id)] 顺序 = 目标顺序
        missing = []  # 所有检测不到的物种id
        for sid in ids:
            cands = by_species.get(sid)
            if not cands:
                missing.append(sid)
                continue
            chosen.append((cands.pop(0), sid))
        if missing:
            # 一次性列出所有找不到的精灵(名称+id), 输出提示并中止脚本运行
            parts = "，".join(f"{_pet_name(sid)}[ID={sid}]" for sid in missing)
            print(f"找不到指定的精灵：{parts}！", flush=True)
            raise SystemExit(1)

        # 2. 当前背包所有精灵存仓库: 第一背包 pos=0, 第二背包 pos=3
        for p in first:
            if p.get("catchTime"):
                self.send(2304, [p["catchTime"], 0]); _t.sleep(0.05)
        for p in second:
            if p.get("catchTime"):
                self.send(2304, [p["catchTime"], 3]); _t.sleep(0.05)

        # 3. 按目标顺序取回: 前6(第一背包 pos=1), 后6(第二背包 pos=2)
        for i, (ct, _sid) in enumerate(chosen):
            pos = 1 if i < 6 else 2
            self.send(2304, [ct, pos]); _t.sleep(0.05)

        # 4. 设首发(第1格 = 列表第1个)
        self.send(2308, [chosen[0][0]]); _t.sleep(0.1)

        # 5. 刷新并摆正顺序 (41462 交换; 双格均占位时最稳)
        self.send(43706)
        bag = self._wait_bag()
        slots = self._slot_catch(bag)
        target = [c for c, _s in chosen]
        for i in range(len(target)):
            want = target[i]
            if slots[i] == want:
                continue
            if want in slots:
                j = slots.index(want)
                sort_i, sort_j = i + 1, j + 1
                if slots[i] == 0:
                    self.send(41462, [sort_j, want, sort_i, 0])
                else:
                    self.send(41462, [sort_i, slots[i], sort_j, want])
                slots[i], slots[j] = want, slots[i]
                _t.sleep(0.05)
        return {"ok": True, "target": ids}

    # ---------- 查找精灵是否存在 (含精英背包) ----------
    def _ensure_exe(self, timeout: float = 15.0):
        """触发后端拉取精英(爱宠)背包(2361)并等数据, 返回 [(catchTime, id), ...]."""
        import time as _t
        try:
            self._post("/api/exe/fetch", {})
        except Exception:
            pass
        start = _t.time()
        last_n, stable = -1, 0
        while _t.time() - start < timeout:
            try:
                j = self._get_json("/api/exe")
            except Exception as e:
                raise SeerError(f"读取精英背包失败: {e}")
            if j.get("fetched"):
                pets = j.get("pets", [])
                n = len(pets)
                if n == last_n:
                    stable += 1
                    if stable >= 2:
                        return [(p.get("catchTime"), p.get("id"))
                                for p in pets if p.get("catchTime")]
                else:
                    stable, last_n = 0, n
            _t.sleep(0.4)
        raise SeerError("等待精英背包数据超时(请确认后端已登录)")

    def find_pet(self, ids) -> dict:
        """查找给定**物种 id** 的精灵是否存在, 及所在位置.

        ids: 物种id 或 物种id列表. 会在三类来源中搜索: 背包(/api/bag, 前6出战/后6待命)、
        仓库(/api/storage, 2303)、以及**精英背包**(/api/exe, 2361 GET_LOVE_PET_LIST)。

        返回 ``{str(id): {"locations": [位置...], "count": n}}``; 位置取值:
        ``背包1(出战)/背包2(待命)/仓库/精英背包``。精英背包也纳入"存在"判断。
        """
        if isinstance(ids, (int, str)):
            ids = [ids]
        ids = [int(x) for x in ids]
        if not ids:
            raise SeerError("ids 不能为空")

        # 刷新并读取三类来源
        try:
            self.send(43706)
        except Exception:
            pass
        bag = self._wait_bag()
        storage = self._ensure_storage()
        exe = self._ensure_exe()

        locs = {sid: {"locations": [], "count": 0} for sid in ids}

        def add(sid, loc):
            if sid in locs:
                locs[sid]["locations"].append(loc)
                locs[sid]["count"] += 1

        for p in bag.get("first", []):
            add(p.get("id"), "背包1(出战)")
        for p in bag.get("second", []):
            add(p.get("id"), "背包2(待命)")
        for _ct, pid in storage:
            add(pid, "仓库")
        for _ct, pid in exe:
            add(pid, "精英背包")
        return {str(k): v for k, v in locs.items()}


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

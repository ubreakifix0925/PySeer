"""PySeer 第三方库: 基于 PySeer 后端 (webui) 的 HTTP API.

后端启动并登录游戏账号后, 脚本通过本库即可让后端发/收包并取值. 本库为 **PySeer** 项目面向
脚本开发者提供的高度可扩展第三方库, 旧名 `seerlib`(仍保留为兼容别名, 内容与本模块一致).

快速示例::

    from PySeer import Seer
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
    get_recv_value(cmd, params, index) -> 发包并等 RECV, 直接取应答包体第 index 个值

高阶: set_bag(ids) <-> 把背包全部切换为指定物种 id 列表 (物理重排, 会发真实游戏命令).
      find_pet(ids) <-> 查找指定物种 id 是否存在, 及所在位置(背包1/背包2/仓库/**精英背包**).
      get_item_count(item_id) <-> 获取指定物品 id 的数量 (发 42399 [1,物品id], 取应答第 3 个参数).
    get_value(body, index) -> 从包体取第 index 个值 (int32 大端)

对战体 (Battle): 以"带 cmdid 的完整 HEX 包"作为进入对战的输入(构造时自动发送并等待进场, 失败抛
    SeerError), 然后**自动按回合推进**: 每个会消耗回合的操作(use_skill/use_item/capture/escape)
    在发包后都会自动等待本回合结算(2505), 因此无需手动等回合; 只有**死亡切换** change_pet 不消耗
    回合, 换上新精灵后可在同一回合内再执行一次操作. 期间可读取当前回合数据(round/my/other 等),
    并用任意复杂的 if/else/循环判断结构驱动决策; 收到结束包(2506)后 finished 置 True, 循环自动终止.
    完整 API 见 docs/PySeer.md.
"""

from __future__ import annotations

import json
import os
import re
import time as _time
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
        # 通用能力: 可由环境变量 PYSEER_ACCOUNT 指定请求附带的标识(由后端扩展件解释, 默认 None).
        self.account = os.environ.get("PYSEER_ACCOUNT") or None
        self.base = discover_backend(base,
                                     probe=probe,
                                     timeout=probe_timeout or _PROBE_TIMEOUT)
        self.timeout = timeout

    # ---------- 底层 HTTP ----------
    def _post(self, path: str, data: dict, *, _retry: bool = True) -> dict:
        """POST 到后端, 并对"游戏连接掉线"做**透明重连重试**.

        若请求因后端游戏连接断开(被动/主动重连中)而失败, 会**阻塞等待**后端恢复上线后再重试同一次请求,
        从而让脚本无需写任何被动断线检测——后端掉线期间脚本自动暂停, 恢复后从断点继续.
        对 "/api/disconnect" 与 "/api/reconnect" 不重试(以免错乱); 真实错误(游戏在线时的失败)直接抛出.
        """
        try:
            return self._post_raw(path, data)
        except SeerError:
            if not _retry or path in ("/api/disconnect", "/api/reconnect"):
                raise
            try:
                # 游戏连接确实掉了 -> 等后端自愈(被动90s/主动重登)后重试一次
                if not self.is_connected() and self._await_backend_recover(timeout=240):
                    return self._post(path, data, _retry=False)
            except Exception:
                pass
            raise

    def _post_raw(self, path: str, data: dict) -> dict:
        if self.account is not None:
            data.setdefault("account", self.account)   # 附加请求标识(若指定)
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

    def _await_backend_recover(self, timeout: float = 240.0) -> bool:
        """后端游戏连接掉线且**正在重连**(被动自愈/主动重登)时, 阻塞等待其恢复上线.

        返回 True 表示连接已恢复(可重试); 返回 False 表示超时或后端根本不在重连(idle/error 等).
        """
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            try:
                st = self._get_json("/api/status")
            except Exception:
                _t.sleep(1.0)
                continue
            if st.get("connected"):
                return True
            if st.get("status") not in ("disconnected", "logging_in"):
                return False            # idle/error -> 不在重连过程, 不该等
            _t.sleep(1.0)
        return False

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

    def get_recv_value(self, cmd, params, index: int, timeout: float = 8.0) -> int:
        """发送命令并等待其 RECV, 直接返回**应答包体**中第 ``index`` 个值 (int32 大端).

        等价于 ``get_value(self.recv(cmd, params), index)`` 的一步封装: 一次调用即拿到
        "发包 → 等该命令应答 → 取应答包体(不含命令号/包头)某个参数序号的值".

        :param cmd: 命令号(或命令名, 如 ``'ENTER_MAP'``)
        :param params: 发送包体(参数列表, 见 spec 语法: 数字→int32, ``s:/b:/h:`` 等)
        :param index: 应答包体的**参数序号**(0 基 int32 索引); 越界抛 ``SeerError``
        :param timeout: 等 RECV 超时(秒), 默认 8
        :return: int
        """
        pkt = self.recv(cmd, params, timeout=timeout)
        return get_value(pkt, index)

    def get_item_count(self, item_id, timeout: float = 8.0) -> int:
        """获取指定**物品 id** 的数量.

        发 ``42399(MULTI_ITEM_LIST)`` 包体 ``[1, 物品id]``(先 1, 后物品 id, 各 int32 大端);
        服务器应答包体(**不含命令号/包头**)按 4 字节大端 int32 拆, 取其**第三个**参数(索引 2)
        即为该物品的数量. 返回 int; 若应答取不到第 3 个参数抛 ``SeerError``.

        :param item_id: 物品 id (int)
        :param timeout: 等 RECV 超时(秒), 默认 8
        :return: 物品数量 (int)
        """
        pkt = self.recv(42399, [1, int(item_id)], timeout=timeout)
        return get_value(pkt, 2)

    # ---------- 换背包 (物种 id -> 物理重排 12 格) ----------
    def _get_json(self, path: str, *, _retry: bool = True) -> dict:
        """GET 请求 -> JSON dict (后端 /api/bag、/api/storage 等).

        与 ``_post`` 一样对"游戏连接掉线"做透明重连重试; 对 "/api/status" 不重试(供状态读取).
        """
        if self.account is not None:
            import urllib.parse as _up
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}account={_up.quote(str(self.account))}"
        try:
            req = urllib.request.Request(self.base + path, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if not _retry or path.startswith("/api/status"):
                raise
            try:
                if not self.is_connected() and self._await_backend_recover(timeout=240):
                    return self._get_json(path, _retry=False)
            except Exception:
                pass
            raise

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

    def find_pet_catchtime(self, ids):
        """查找指定**物种 id** 的精灵, 返回所持有的 **catchTime 列表**(同物种可能多只).

        数据来源与 ``find_pet`` 完全一致: 背包(/api/bag, 前6出战/后6待命, 由 43706 刷新)、
        仓库(/api/storage, 2303)、以及**精英背包**(/api/exe, 2361 GET_LOVE_PET_LIST)。
        区别: ``find_pet`` 只返回"位置", 本函数把每个精灵的 **catchTime** 也带出来——
        供按 catchTime 定位的场景(如提交远征阵容 42127、按 catchTime 换宠等)使用。

        **返回(按输入决定)**:
        - 传**单个** id(int/str) -> 直接返回该物种的 ``[catchTime, ...]`` 列表(可能有空)。
        - 传**列表** ``[id1, id2]`` -> 返回 ``{str(物种id): [catchTime, ...]}``。

        :param ids: 单个物种 id, 或 物种 id 列表(int/str/混合).
        :return: 单个 id -> ``list[int]``; 列表 -> ``{str(id): list[int]}``。
                 列表已去重(同一 catchTime 只留一次), 顺序为 背包(出战/待命) -> 仓库 -> 精英背包。
        """
        multi = isinstance(ids, (list, tuple))
        if not multi:
            ids = [ids]
        ids = [int(x) for x in ids]
        if not ids:
            raise SeerError("ids 不能为空")

        # 刷新并读取三类来源 (与 find_pet 相同)
        try:
            self.send(43706)
        except Exception:
            pass
        bag = self._wait_bag()
        storage = self._ensure_storage()
        exe = self._ensure_exe()

        res = {sid: [] for sid in ids}

        def add(sid, ct):
            if ct and sid in res:
                res[sid].append(int(ct))

        for p in bag.get("first", []):
            add(p.get("id"), p.get("catchTime"))
        for p in bag.get("second", []):
            add(p.get("id"), p.get("catchTime"))
        for ct, pid in storage:
            add(pid, ct)
        for ct, pid in exe:
            add(pid, ct)

        # 去重保序: 同一 catchTime 只留一次
        for sid in ids:
            res[sid] = list(dict.fromkeys(res[sid]))
        if multi:
            return {str(sid): res[sid] for sid in ids}
        return res[ids[0]]

    # ---------- 断线检测 / 断线重连 (对接后端 /api/disconnect, /api/reconnect) ----------
    def is_connected(self) -> bool:
        """当前游戏连接是否在线(后端 /api/status 的 ``connected``).

        True = 后端已登录且游戏 socket 开启; False = 掉线/未登录.
        """
        j = self._get_json("/api/status")
        return bool(j.get("connected"))

    def drop_connection(self) -> dict:
        """强制断开当前连接(后端 /api/disconnect).

        用途: 战斗中主力阵亡时**立刻断线**以中止对局——赶在"投降/全员战败(2506)"之前, 让主力
        不被判死; 紧接着可调 ``reconnect()`` 重登后重打同一关.
        """
        j = self._post("/api/disconnect", {})
        if not j.get("ok"):
            raise SeerError(j.get("error", "断开失败"))
        return j

    def reconnect(self, timeout: float = 30.0) -> dict:
        """[主动] 断线重连: 若当前在线先断开(中止对局, 避免主力死亡被提交), 再让后端重新登录.

        成功后**轮询 /api/status 直到 ``status==ready`` 且 ``connected``**, 才返回; 超时抛 SeerError.
        返回后端的最终状态 dict. 供"主力阵亡 -> 立刻断线 -> 重连 -> 重打同一关"这类**主动中断**使用.

        注意: **被动掉线**的检测与"隔 90s 再自动重连"由**后端**完成(见 webui 的
        ``_schedule_passive_reconnect``), 脚本不需要也不应在这里等待; 脚本只需在需要继续时
        ``wait_until_connected()`` 等后端自愈即可.
        """
        import time as _t
        # 主动: 若已在线上, 先断(中止当前对局, 避免主力死亡被提交)
        try:
            if self.is_connected():
                self._post("/api/disconnect", {})
        except Exception:
            pass
        j = self._post("/api/reconnect", {})
        if not j.get("ok"):
            raise SeerError(j.get("error", "重连失败") or "后端拒绝重连")
        # 轮询等待重登完成
        end = _t.time() + timeout
        status = ""
        while _t.time() < end:
            try:
                st = self._get_json("/api/status")
            except Exception:
                _t.sleep(1.0)
                continue
            status = st.get("status", "")
            if status == "ready" and st.get("connected"):
                return st
            if status == "error":
                raise SeerError(f"重连失败(后端状态=error): {st.get('detail')}")
            _t.sleep(0.8)
        raise SeerError(f"重连超时({timeout}s): 状态停在 {status or '?'}")

    def wait_until_connected(self, timeout: float = 120.0) -> dict:
        """阻塞直到后端重新在线(被动掉线自愈后恢复). 返回最终状态 dict.

        配合后端"被动掉线自动重连"使用: 掉线后后端会隔 ``PASSIVE_RECONNECT_WAIT``(默认90s)自动重登,
        脚本只需调用本方法等待它自愈回来, 即可**继续之前的工作**, 无需脚本再实现 90s/重连逻辑.
        超时抛 SeerError.
        """
        import time as _t
        end = _t.time() + timeout
        last = {}
        while _t.time() < end:
            try:
                st = self._get_json("/api/status")
            except Exception:
                _t.sleep(1.0)
                continue
            last = st
            if st.get("connected"):
                return st
            if st.get("status") == "error":
                raise SeerError(f"后端状态=error, 无法等待就绪: {st.get('detail')}")
            _t.sleep(1.0)
        raise SeerError(f"等待后端重连超时({timeout}s), 状态: {last.get('status')}")


# ---------- 对战体 (Battle) ----------
class Battle:
    """对战体: 绑定已登录后端的一场对战会话, **自动按回合推进**, 无需手动等待回合或进场.

    以**带 cmdid 的完整 HEX 包**作为进入对战的输入: ``Battle(hex_packet)`` 构造时会自动发送该包
    并等服务端下发进场数据(2503 出场队伍 / 2504 当前出战); 若无法正常进入(超时/收到结束包),
    直接抛 ``SeerError``. 进场成功后即可立即按回合操作.

    **操作即回合**的模型(与你的理解一致): 每个会消耗回合的操作(``use_skill``/``use_item``/
    ``capture``/``escape``)在发包后都会**自动等待该回合结算(2505)**并返回, 因此你**不需要**再写
    ``wait``/``wait_round``. 唯一例外是**死亡切换** ``change_pet``(当前精灵阵亡时的强制换宠): 它只把
    新精灵换上而不消耗回合, 之后可在同一个回合内继续执行一次操作. 收到结束包(2506 FIGHT_OVER)
    后 ``finished`` 置 True, 循环自动终止.

    后端(webui)在后台已把 2503/2504/2505/2506/2407/2406/2409 等对战包解析进统一对战状态
    (``_BATTLE``), 本类只是再提供一层"自动回合"的脚本驱动封装.

    用法示例::

        from seerlib import Battle
        battle = Battle("带cmdid的完整HEX包")   # 发送对战包 + 自动进场; 失败抛 SeerError
        while not battle.finished:
            my, other = battle.my, battle.other
            # —— 任意复杂的判断结构 ——
            if my and (my.get('hp') or 0) <= 0:
                battle.change_pet(battle.my_team[1]['id'])  # 死亡切换(传物种id), 不消耗回合
                battle.use_skill(battle.skills[0])                  # 同一回合内继续出招
            elif my and (my.get('hp') or 0) < 300:
                battle.use_item(300014)                             # 用道具(消耗一回合)
            else:
                battle.use_skill(battle.skills[0])                  # 使用技能(消耗一回合)
            rnd = battle.round                                      # 本回合(2505)数据
            print(rnd.get('first', {}).get('lostHP'))               # 例如读取本回合伤害
    """

    # 进场"稳定性窗口": 对战状态刚变就绪时, 再连续稳定该时长(秒)内无回退才判定"成功发起".
    # 防止误判: 只收到 2503(队伍, my/other=队伍首只) 或瞬态就绪时, 不会立刻当作可操作.
    ENTRY_SETTLE = 0.8

    @staticmethod
    def _battle_ready(snap) -> bool:
        """判定对战是否已具备"可操作"的基础状态: 进行中 + 双方当前精灵都在.

        放宽到不要求双方队伍列表(某些模式的 2503 未必给出完整队伍, 但双方当前出战仍会来),
        配合 ENTRY_SETTLE 稳定窗口, 既能防"2503 半开场就误判", 又不会因队伍缺失而进场超时.
        """
        if not snap:
            return False
        return bool(snap.get("active") and snap.get("my") and snap.get("other"))

    def __init__(self, hex_packet=None, base=None, timeout: float = 30.0, probe: bool = True,
                 entry_timeout: float = 15.0):
        self._seer = Seer(base=base, timeout=timeout, probe=probe)
        self.entry_timeout = entry_timeout   # 进入对战/单次等待的超时
        self._hex = hex_packet
        self._version = 0         # 已观察到的后端对战版本号 (用于 wait 判断"新事件")
        self._snap = {}           # 最近一次 _BATTLE 快照
        self._finished = False    # 是否已收到结束包(2506)
        self._events = []         # 事件记录: [{version, cmd, ts}]
        self._last_my = None      # 记录最后(结账前)的我方当前精灵, 便于结束后仍可读
        self._last_other = None
        if hex_packet:
            self.start(hex_packet)

    # ---------- 进入对战 / 读取快照 ----------
    def _fetch(self) -> dict:
        """GET /api/battle, 返回后端当前对战快照(dict)."""
        return self._seer._get_json("/api/battle")

    def start(self, hex_packet=None, entry_timeout: float = None) -> dict:
        """发送"带 cmdid 的完整 HEX 包"进入对战, 并**充分等待对战成功发起**; 失败抛 SeerError.

        - 若发送前后端**已在对战中**(active + 双方当前精灵), 视为已就绪, 直接返回当前状态,
          不重复等待(避免把"已有对战"误判成"尚未进入")。
        - 否则发送触发包后, 等待到对战状态**连续稳定**进入"可操作"态(见 ``_wait_entry``),
          防止仅凭 2503(队伍) 或瞬态就误判为已发起。
        """
        if hex_packet is not None:
            self._hex = hex_packet
        if not self._hex:
            raise SeerError("对战体需要一个带 cmdid 的完整 HEX 包作为对战包输入")
        pre = self._fetch() or {}                     # 发送前状态
        base_ver = int(pre.get("version", 0))
        self._version = base_ver
        self._snap = pre
        if self._battle_ready(pre):                   # 本来就在对战 -> 直接认为已就绪
            return pre
        j = self._seer._post("/api/battle/hex", {"hex": self._hex})
        if not j.get("ok"):
            raise SeerError(j.get("error", "发送对战进入包失败"))
        return self._wait_entry(entry_timeout)

    # ---------- 等待/推进 ----------
    def wait(self, timeout: float = 8.0):
        """阻塞直到对战状态发生变化(新事件)或对**战结束**; 返回最新快照(dict).

        超时返回 None(此时段无新对战事件). 用于让脚本按回合推进: 例如发技能后调用,
        会阻塞到服务端回发 2505 回合结果(或 2506 结束包).
        """
        j = self._seer._post("/api/battle/wait", {"version": self._version, "timeout": timeout})
        if not j.get("ok"):
            raise SeerError(j.get("error", "wait 失败"))
        if not j.get("changed"):
            return None                     # 超时: 该时段无新对战事件
        b = j.get("battle") or {}
        self._snap = b
        self._version = max(self._version, int(b.get("version", 0)))
        self._finished = bool(b.get("finished", False))
        self._record_event(b)
        return b

    def wait_active(self, timeout: float = 15.0):
        """阻塞直到进入对战(收到 2503, active=True), 返回最新快照; 超时抛 SeerError."""
        end = _time.time() + timeout
        while _time.time() < end:
            self.wait(2.0)
            if self._snap.get("active") or self._finished:
                return self._snap
        raise SeerError("等待进入对战超时 (未收到 2503 出场队伍)")

    def wait_round(self, timeout: float = 15.0):
        """阻塞直到收到一回合结果(2505 NOTE_USE_SKILL)或对**战结束**, 返回该回合快照; 超时抛 SeerError.

        会自动跳过非回合事件(如 2404 应答/2507 更新等), 直到真正解出一回合.
        """
        end = _time.time() + timeout
        while _time.time() < end:
            self.wait(2.0)
            if self._finished:
                return self._snap
            if (self._snap or {}).get("lastCmd") == 2505:
                return self._snap
        raise SeerError("等待回合结果(2505)超时")

    # ---- 内部: 面向"自动回合"的等待 ----
    def _wait_entry(self, timeout: float = None):
        """等对战**充分发起**并稳定, 才返回; 防止误判.

        避免把**上一场对战遗留的 ``finished=True``**(或开盘前的瞬态) 误判成"本场进入失败":
        - 在**本场对战真正进入之前**(``entered`` 为 False), 若看到 ``finished``, 一律忽略并继续等
          新的 2503(它会把它复位为 False 并置 active=True); 只有超时仍未进入才抛 SeerError。
        - 一旦本场已进入(``active``+双方当前精灵+双方队伍), 再要求该状态**连续稳定 ``ENTRY_SETTLE``
          秒**无回退; 若进入后立刻收到结束包(2506), 抛 SeerError("收到了结束包")。
        """
        timeout = timeout or self.entry_timeout
        end = _time.time() + timeout
        stable_since = None
        entered = False                         # 是否已看到"本场对战"真正进入(active+就绪)
        while _time.time() < end:
            self.wait(0.4)                      # 推进到下一个对战事件(或超时)
            snap = self._snap
            if self._finished:
                if entered:
                    raise SeerError("对战未能正常进入(收到了结束包)")
                # 未进入就收到结束标志: 可能是上一场遗留, 忽略并继续等本场 2503
                stable_since = None
                _time.sleep(0.05)
                continue
            if self._battle_ready(snap):
                entered = True
                if stable_since is None:
                    stable_since = _time.time()  # 首次进入就绪态, 开始计稳定窗口
                elif (_time.time() - stable_since) >= self.ENTRY_SETTLE:
                    return snap                 # 已稳定 ENTRY_SETTLE 秒 -> 成功发起
            else:
                stable_since = None             # 状态不完整/回到未就绪 -> 重置稳定计时
        raise SeerError(
            f"进入对战超时({timeout}s): 未收到 2503 出场队伍或 2504 开场; 请确认对战触发 HEX 包有效")

    def _wait_finish(self, timeout: float = None):
        """内部: 等对战结束包(2506); 超时抛 SeerError."""
        timeout = timeout or self.entry_timeout
        end = _time.time() + timeout
        while _time.time() < end:
            self.wait(1.5)
            if self._finished:
                return self._snap
        raise SeerError("等待对战结束(2506)超时")

    def _after_round(self, settle: float = 0.6):
        """回合结算后: 若某一方队伍**全体阵亡**(终局回合), 再短暂等待随后的结束包(2506).

        这样"真正分出胜负的那一回合"结束后 ``finished`` 就已置 True, 循环不会再误发一次多余操作.
        单只精灵阵亡(body `remainHP==0`)只是触发死亡切换/换宠, 并不等于终局, 不在这里多等.
        """
        if self._finished:
            return
        snap = self._snap

        def side_dead(team):
            if not team:
                return False
            known = [p for p in team if p.get("hp") is not None]
            if not known or len(known) < len(team):
                return False            # 有未知血量的精灵时不判定, 避免误判
            return all((p.get("hp") or 0) <= 0 for p in known)

        if side_dead(snap.get("myTeam")) or side_dead(snap.get("otherTeam")):
            self.wait(settle)

    def _record_event(self, b):
        cmd = b.get("lastCmd")
        self._events.append({"version": b.get("version"), "cmd": cmd,
                             "ts": _time.strftime("%H:%M:%S")})
        # 结束后端把 my/other 清空, 这里保留最后一帧供脚本回读最终状态
        if b.get("my") is not None:
            self._last_my = b.get("my")
        if b.get("other") is not None:
            self._last_other = b.get("other")

    def run(self, decide, timeout: float = 15.0) -> bool:
        """自动驱动整场对战, 直到收到**结束包(2506)** 后返回 True.

        ``decide(this)`` 是每回合的**决策回调**: 每回合对战体已更新好状态(可直接读 ``this.my``/
        ``this.other``/``this.round``/``this.skills``), 回调里**决定并发出本回合动作**(如
        ``this.use_skill(...)``; 若当前精灵阵亡可先 ``this.change_pet(...)`` 再 ``this.use_skill``;
        想逃跑可 ``this.escape()``). 因为每个动作都会**自动等待回合结算**, 所以本方法只需循环调用
        ``decide`` 直到 ``finished`` —— 你只写判断逻辑, 不用理会何时等回合/何时进场.

        注意: ``decide`` 每回合至少要发一个**消耗回合**的动作(use_skill/use_item/capture/escape),
        否则会原地空转.
        """
        if not self.active:
            raise SeerError("对战尚未进入(请用 Battle(hex) 构造或先调用 start(hex))")
        while not self.finished:
            decide(self)
        return self.finished

    def __repr__(self):
        return (f"<Battle active={self.active} finished={self.finished} "
                f"version={self._version} last_cmd={self.last_cmd}>")

    # ---------- 读: 当前对战 / 回合数据 ----------
    @property
    def state(self) -> dict:
        """当前对战快照(dict): {active, finished, mode, my, other, myTeam, otherTeam, ...}."""
        return self._snap

    @property
    def finished(self) -> bool:
        """是否已收到结束包(2506 FIGHT_OVER), 对战体据此终止."""
        return self._finished

    @property
    def active(self) -> bool:
        return bool(self._snap.get("active"))

    @property
    def last_cmd(self):
        return self._snap.get("lastCmd")

    @property
    def version(self) -> int:
        return self._version

    @property
    def mode(self):
        return self._snap.get("mode")

    @property
    def my(self):
        """我方当前出战精灵(dict), 结束后返回最后一帧."""
        return self._snap.get("my") or self._last_my

    @property
    def other(self):
        """敌方当前出战精灵(dict), 结束后返回最后一帧."""
        return self._snap.get("other") or self._last_other

    @property
    def my_team(self) -> list:
        return self._snap.get("myTeam") or []

    @property
    def other_team(self) -> list:
        return self._snap.get("otherTeam") or []

    @property
    def skills(self) -> list:
        """我方当前出战精灵可用的技能 id 列表."""
        return self._snap.get("mySkills") or []

    @property
    def round(self):
        """当前回合数据: 2505 NOTE_USE_SKILL 的解析结果(dict) 或 None.

        内含 first/second 两个 AttackValue(我方/敌方)、hpUpdates、skillRecords、
        attackBlocks、endOffset 等, 详见 seer/fightinfo.py::parse_note_use_skill.
        """
        return self._snap.get("lastSkill")

    @property
    def report(self) -> list:
        """后端战报记录(chronological [{t,msg}])."""
        return self._snap.get("report") or []

    @property
    def events(self) -> list:
        """本对战体观察到的每个事件: [{version, cmd, ts}]."""
        return list(self._events)

    # ---------- 操作: 发包 / 用技能 / 换宠 / 用道具 / 捕捉 / 逃跑 ----------
    def send(self, cmd, params=None, encode: str = "pack") -> dict:
        """发送任意对战命令(可为命令号或命令名), params 为参数列表(默认打包为 int32 包体).

        encode="hex" 时把 params 原样当作十六进制包体下发. 返回后端应答 dict.
        """
        j = self._seer._post("/api/battle/send", {
            "cmd": str(cmd), "body": self._seer._spec(params), "encode": encode})
        if not j.get("ok"):
            raise SeerError(j.get("error", "对战发包失败"))
        return j

    def send_hex(self, hex_packet: str) -> dict:
        """发送一条带 cmdid 的完整 HEX 包; 后端会重建 uid/序列号并加密封包下发."""
        j = self._seer._post("/api/battle/hex", {"hex": hex_packet})
        if not j.get("ok"):
            raise SeerError(j.get("error", "发送 HEX 包失败"))
        return j

    def use_skill(self, skill_id, timeout: float = None) -> dict:
        """使用技能(2405): 发包后**自动等待本回合结算(2505)**, 即"一个操作=过一回合".

        返回本回合后的最新对战快照(含 ``round``/``my``/``other``). 若本回合为终局回合,
        会顺带等到结束包(2506)并把 ``finished`` 置 True.
        """
        self.send(2405, [skill_id])
        snap = self.wait_round(timeout or self.entry_timeout)
        self._after_round()
        return snap

    def use_item(self, item_id, catchTime=None, timeout: float = None) -> dict:
        """用道具(2406): 发包后**自动等待本回合结算(2505)**, 消耗一回合.

        2406 的包体是 **[我方当前出战精灵 catchTime, 物品id, 0]** 三个 int32
        (依客户端 ``RenewBloodItemCategory.as``::
        ``send(USE_PET_ITEM, playerMode.info.catchTime, itemID, 0)``)。
        只发物品 id 会被服务端判为非法操作 —— 实测会立刻回 2506 FIGHT_OVER
        并**断开连接**。故这里默认自动取当前出战精灵的 catchTime 补齐;
        也可显式传 ``catchTime=``。

        :param item_id: 物品 id (如 300014 超级体力药剂)
        :param catchTime: 目标精灵 catchTime; 默认取 ``my.catchTime``
        """
        if catchTime is None:
            catchTime = (self._snap.get("my") or {}).get("catchTime")
        if not catchTime:
            raise SeerError("取不到我方当前出战精灵的 catchTime, 无法用药 (未在对战中?)")
        self.send(2406, [int(catchTime), int(item_id), 0])
        snap = self.wait_round(timeout or self.entry_timeout)
        self._after_round()
        return snap

    def capture(self, *params, timeout: float = None) -> dict:
        """捕捉(2409): 发包后**自动等待本回合结算(2505)**, 消耗一回合."""
        self.send(2409, list(params))
        snap = self.wait_round(timeout or self.entry_timeout)
        self._after_round()
        return snap

    def change_pet(self, species_id, catchTime=None, *, death: bool = None,
                   timeout: float = None) -> dict:
        """换宠(2407): 既可作为**死亡切换**(不消耗回合), 也可作为**主动切换**(消耗一回合).

        - 推荐传 **物种 id**(如 ``battle.change_pet(5000)``): 后端会从**当前对战阵容**(``myTeam``)
          里查一只该 id 的可用精灵, 取其 ``catchTime`` 发包; 避免脚本里手填 catchTime(那个值很难拿对)。
        - 也可传 ``catchTime=目标精灵catchTime`` 直接指定(后端用它发包)。

        ``death`` 控制"这个切换是否消耗回合":
        - ``None``(默认): **自动判断**——当前我方出战精灵阵亡(``my.hp<=0`` 或未知) → **死亡切换**,
          不消耗回合(换完可继续出招); 否则(精灵还活着) → **主动切换**, 消耗一回合。
        - ``True``: 强制**死亡切换**(不消耗回合)。
        - ``False``: 强制**主动切换**(消耗一回合, 换完后等待本回合结算 2505)。

        两种情况都会发 ``2407`` + 目标精灵 catchTime, 然后**等到新精灵真正成为我方当前出战精灵**
        (``my.catchTime`` 发生变化) 并把 ``my``/``skills`` 刷新为它. 作为主动切换时, 还会继续等一个
        回合结果(2505), 以便调用方知道这一回合已被这次换宠消耗掉.

        之所以等"状态变化"而不是 ``lastCmd==2407``: 后端的 2407 应答可能被紧随其后的回合包(如 2505)
        覆盖(``lastCmd`` 变成 2505), 或对端(NPC)换宠(userID==0)不更新我方 ``my``; 这些都可能导致
        误判超时. 等到 ``my.catchTime`` 变成目标精灵是**权威**的"已换上"信号.

        返回换宠后的最新对战快照(``my``/``skills`` 已更新为新精灵).
        """
        if catchTime is not None:
            payload = {"catchTime": int(catchTime)}      # 直接指定 catchTime
        else:
            payload = {"id": int(species_id)}            # 按物种 id, 后端从阵容查 catchTime
        prev_ct = (self._snap.get("my") or {}).get("catchTime")   # 我方当前出战 catchTime
        # 主动切换判定: 默认按当前精灵存活情况自动判断
        if death is None:
            _hp = (self._snap.get("my") or {}).get("hp")
            death = True if (_hp is None or _hp <= 0) else False
        j = self._seer._post("/api/battle/change-pet", payload)
        if not j.get("ok"):
            raise SeerError(j.get("error", "换宠失败"))
        # 等新精灵上场 (my.catchTime 变化)
        end = _time.time() + (timeout or self.entry_timeout)
        while _time.time() < end:
            self.wait(1.0)
            if self._finished:
                return self._snap
            my_ct = (self._snap.get("my") or {}).get("catchTime")
            if my_ct is not None and my_ct != prev_ct:         # 新精灵已上场
                break
        else:
            raise SeerError(f"换宠超时: 未收到新精灵上场 (目标 id={species_id} catchTime={catchTime})")
        if death:
            return self._snap                                  # 死亡切换: 不消耗回合
        # 主动切换: 消耗一回合 -> 等本回合结算(2505), 并处理终局
        snap = self.wait_round(timeout or self.entry_timeout)
        self._after_round()
        return snap

    def escape(self, timeout: float = None) -> dict:
        """逃跑(2410): 发包后**自动等待对战结束包(2506)**. 返回结束后的快照."""
        self.send(2410, [])
        return self._wait_finish(timeout or self.entry_timeout)

    def act(self, msg: str) -> dict:
        """把一条脚本动作记入后端战报(便于观察/回放). 返回后端应答 dict."""
        return self._seer._post("/api/battle/action", {"msg": str(msg)})


# ---------- 取值函数 (模块级) ----------
def get_value(body, index: int) -> int:
    """从包体取第 index 个值(int32 大端). body 可为 Packet / hex str / bytes.

    越界统一抛 ``SeerError``.
    """
    if isinstance(body, Packet):
        if index < 0 or index >= len(body.ints):
            raise SeerError(f"取值索引 {index} 越界 (包体共 {len(body.ints)} 个 int32)")
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

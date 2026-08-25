"""赛尔号登录客户端: 连接网关、发送登录/心跳封包、解析响应.

流程 (与 seerNew / 52pojie 逆向一致):

    1. 获取淘米 session   (session.get_session)
    2. 解析网关地址       (GET /online_gate -> ws://host:port)
    3. 建立 WebSocket 连接
    4. 发送登录封包 (cmd 1001 = login)
    5. 等待服务器登录应答 (cmd 1001) -> 记录序列号
    6. 开始心跳 (cmd 1002) 与时间校验应答

此外支持"游戏服务器裸 TCP 加密登录"(登录器/Flash 客户端方式):
    连接 101.43.19.60:1201, 发送加密登录封包 (cmd 1001, 包体含 "flash_taomee"),
    收到服务器角色数据. 所有封包均被 seer 算法加密.
"""

import hashlib
import struct

from .misc import binary_to_hex, decimal_to_8hex, hex_to_bytearray
from .packet import CMD_LOGIN, PacketData, compute_result, decrypt, encrypt, parse_packet
from .session import ACCOUNT_AUTH_URL, _http_get, get_session
from .tcp_client import TCPClient
from .ws_client import WSClient, WebSocketClosed, WebSocketTimeout

# 默认网关入口: 返回形如 host:port (或 ws/wss url) 的文本
DEFAULT_GATEWAY_ENDPOINT = "https://seerh5login.61.com/online_gate"

# 登录包体尾部 (逆向得到的固定内容, 来自 seerNew), 头部 = session(16B) + 该尾部(136B)
LOGIN_BODY_TAIL = (
    "74616f6d65650000000000000000000000000000000000000000000000000000"  # bytes 0-31   "taomee" + padding
    "0000000000000000000000000000000000000000000000000000000000000000"  # bytes 32-63  padding
    "000003ee00000000504300000000000000000000000000000000000000000001"  # bytes 64-95  0x03ee, "PC"
    "000000010000000168355f7765625f74616f6d65650000000000000000000000"  # bytes 96-127 "h5_web_taomee"
    "0000000000000000"  # bytes 128-135 padding
)

# 游戏服务器 (裸 TCP 加密登录) 的登录包体尾 (108B):
# "unknown"(7B) + 0x00*57 + [1,1,1]*4B(12B) + "flash_taomee"(12B) + 0x00*20
GAME_LOGIN_TAIL = (
    b"unknown" + b"\x00" * 57
    + (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
    + b"flash_taomee" + b"\x00" * 20
)
assert len(GAME_LOGIN_TAIL) == 108

# 默认游戏服务器 (抓包得到)
DEFAULT_GAME_SERVER = ("101.43.19.60", 1201)

# 通信密钥: 命令号>=1000 的封包用 seer 算法加密; 登录(1001)之后会切换为会话密钥
DEFAULT_KEY = "!crAckmE4nOthIng:-)"

# ---- 封包头 ver 字节 = 包类型/方向标记 (实测对战抓包得出, 并非版本号) ----
# 客户端->服务器 一律 0x31; 服务器->客户端 分两类: 0x01 = NOTE/主动推送, 0x3E = 对客户端请求的直接应答.
VER_CLIENT_REQ  = 0x31   # 客户端请求 (SEND)
VER_SERVER_NOTE = 0x01   # 服务器主动推送 (NOTE, 如 NOTE_READY_TO_FIGHT / PET_BOOK_UPDATE / LOAD_PERCENT)
VER_SERVER_RESP = 0x3E   # 服务器对客户端某条请求的直接应答 (如 MIBAO_FIGHT / USE_SKILL / READY_TO_FIGHT)


def _ver_kind(ver: int) -> str:
    """按 ver 字节判定包类型: 'request' | 'note' | 'response' | 'unknown'.

    request  = 客户端发给服务器的请求(收包函数一般不会收到);
    note     = 服务器主动推送的 NOTE (ver=0x01);
    response = 服务器对客户端某条请求的直接应答 (ver=0x3E);
    unknown  = 其它/未识别.
    """
    if ver == VER_CLIENT_REQ:
        return "request"
    if ver == VER_SERVER_NOTE:
        return "note"
    if ver == VER_SERVER_RESP:
        return "response"
    return "unknown"


class LoginError(Exception):
    pass


class SeerClient:
    def __init__(self, account, password, auth_url=None, gateway_endpoint=None,
                 connect_url=None, timeout=12):
        self.account = str(account).strip()
        self.password = password
        self.auth_url = auth_url or ACCOUNT_AUTH_URL
        self.gateway_endpoint = gateway_endpoint or DEFAULT_GATEWAY_ENDPOINT
        # 直接给出的 WebSocket 地址 (跳过 online_gate 解析)
        self.connect_url = connect_url
        self.timeout = timeout

        self.uid_hex = decimal_to_8hex(int(self.account))  # userId 字段固定为 8 位十六进制 (4 字节)
        self.session = None
        self.ws = None
        self.tcp = None            # 游戏服务器裸 TCP 连接
        self.last_result = 0
        self.is_logged_in = False
        self.received_cmds = []
        # 会话密钥: 登录(1001)响应之后按帖子规则派生, 之后所有封包用它解密
        self.session_key = None
        # 可选的帧回调: on_frame(direction, hex_str, cmd_id, body_hex)
        self.on_frame = None

    # ---- 1. session ----
    def fetch_session(self) -> str:
        self.session = get_session(self.account, self.password, self.auth_url, self.timeout)
        return self.session

    # ---- 2. 网关 ----
    def resolve_gateway(self) -> str:
        if self.connect_url:
            return self.connect_url
        text = _http_get(self.gateway_endpoint, self.timeout).strip()
        url = text if text.startswith(("ws://", "wss://")) else "ws://" + text
        if not url.startswith(("ws://", "wss://")):
            raise LoginError(f"网关返回了无法识别的地址: {text!r}")
        return url

    # ---- 3. 连接 ----
    def connect(self):
        url = self.resolve_gateway()
        self.ws = WSClient(url, timeout=self.timeout)
        self.ws.connect()
        return url

    # ---- 封包构建 ----
    def build_login_packet(self) -> str:
        if not self.session:
            raise LoginError("尚未获取 session, 请先调用 fetch_session()")
        body = self.session + LOGIN_BODY_TAIL
        pkt = PacketData(length="00000000", version="31",
                         cmd_id=f"{CMD_LOGIN:08x}", user_id=self.uid_hex,
                         result="00000000", body=body)
        pkt.update_length()
        return pkt.to_hex()

    def build_heartbeat_packet(self, cmd_id=0x3EA) -> str:
        hdr = f"00000011" + "31" + f"{cmd_id:08x}" + self.uid_hex + f"{self.last_result & 0xFFFFFFFF:08x}"
        return hdr

    def build_time_check_response(self, body: str) -> str:
        # 复刻 seerNew 的 SYSTEM_TIME_CHECK: 固定头 + 服务器给的时间体
        return "00000015310000a10c12312c6700000000" + body

    # ---- 发送 ----
    def _send_packet(self, hex_string: str):
        if not self.ws or not self.ws.is_open():
            raise LoginError("WebSocket 未连接")
        pkt = parse_packet(hex_string)
        # 计算并写入序列号 (result 字段)
        self.last_result = compute_result(self.last_result, pkt.byte_body, pkt.cmd_id) & 0xFFFFFFFF
        pkt.result = f"{self.last_result:08x}"
        pkt.user_id = self.uid_hex
        pkt.update_length()
        frame = pkt.to_hex()
        self.ws.send_binary(hex_to_bytearray(frame))
        self._emit_frame("SEND", frame, int(pkt.cmd_id, 16), pkt.body)
        return frame

    def _emit_frame(self, direction, hex_str, cmd_id, body):
        if self.on_frame:
            try:
                self.on_frame(direction, hex_str, cmd_id, body)
            except Exception:
                pass

    def send_login(self):
        self._send_packet(self.build_login_packet())

    def send_heartbeat(self):
        self._send_packet(self.build_heartbeat_packet())

    def send_time_check(self, body: str):
        self._send_packet(self.build_time_check_response(body))

    # ---- 接收 ----
    def recv_packet(self, timeout=None):
        """接收一帧 -> 解析为 PacketData; 若收到登录应答则更新登录状态.

        超时 (窗口内无数据) 返回 None; 连接被对端关闭则抛 WebSocketClosed.
        """
        try:
            opcode, payload = self.ws.recv(timeout=timeout)
        except WebSocketTimeout:
            return None
        if opcode != 0x2:
            return None
        hex_str = binary_to_hex(payload)
        pkt = parse_packet(hex_str)
        self.received_cmds.append(int(pkt.cmd_id, 16))
        self._emit_frame("RECV", hex_str, int(pkt.cmd_id, 16), pkt.body)
        if int(pkt.cmd_id, 16) == CMD_LOGIN:
            # 服务器登录应答, 用它给出的序列号作为后续发送起点
            self.last_result = int(pkt.result, 16) & 0xFFFFFFFF
            self.is_logged_in = True
        return pkt

    def close(self):
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.tcp:
            self.tcp.close()
            self.tcp = None

    # ---- 游戏服务器裸 TCP 加密登录 ----
    def _apply_key(self, key):
        """把 seer 算法库的全局密钥切换为 key (str 或 bytes)."""
        from . import algorithm
        algorithm._key = key.encode("utf-8") if isinstance(key, str) else bytes(key)

    def derive_session_key(self, body) -> str:
        """按参考帖规则派生登录后的会话密钥.

        规则: 取 LOGIN_IN(1001)响应明文的最后4字节作为 uint, 与米米号异或,
        异或值取十进制字符串, 计算其 MD5, 取 hex 前10位作为新通信密钥.
        """
        body = bytes(body)
        seed = int.from_bytes(body[-4:], "big")
        xorv = seed ^ (int(self.account) & 0xFFFFFFFF)
        key = hashlib.md5(str(xorv).encode("utf-8")).hexdigest()[:10]
        self.session_key = key
        self._apply_key(key)
        return key

    def connect_game(self, host=None, port=None, timeout=None):
        """连接游戏服务器 (默认 101.43.19.60:1201), 返回 host:port."""
        host, port = host or DEFAULT_GAME_SERVER[0], str(port or DEFAULT_GAME_SERVER[1])
        self.tcp = TCPClient(host, int(port), timeout or self.timeout)
        self.tcp.connect()
        return f"{host}:{port}"

    def build_game_login_plaintext(self, session=None, result=0) -> bytes:
        """构造游戏登录的明文封包 (Whole packet):

        [PlainLen(4)][ver=0x31][cmd=1001][uid(4)][result(4)][session(16)][flash尾(108)]
        """
        if session is None:
            if not self.session:
                raise LoginError("尚未获取 session")
            session = self.session
        sess = bytes.fromhex(session) if isinstance(session, str) else bytes(session)
        body = sess + GAME_LOGIN_TAIL
        plain_len = 17 + len(body)
        payload = (struct.pack(">I", plain_len)
                   + bytes([0x31])
                   + struct.pack(">I", CMD_LOGIN)
                   + struct.pack(">I", int(self.account))
                   + struct.pack(">I", result & 0xFFFFFFFF)
                   + body)
        return payload

    def send_game_login(self, session=None) -> bytes:
        """构建并发送加密的游戏登录封包, 返回实际下发的 wire 封包."""
        payload = self.build_game_login_plaintext(session)
        # 用包体算序列号 (与抓包一致: MSerial(0, len(body), xor, cmd))
        body = bytes(payload[17:])
        c = 0
        for b in body:
            c ^= b
        self.last_result = compute_result(0, body, f"{CMD_LOGIN:08x}") & 0xFFFFFFFF
        # 重写 result 字段
        payload = payload[:13] + struct.pack(">I", self.last_result) + payload[17:]
        self._apply_key(self.session_key or DEFAULT_KEY)   # 登录用默认密钥
        wire = encrypt(payload)
        self.tcp.send(wire)
        self._emit_frame("SEND", wire.hex(), CMD_LOGIN, body.hex())
        return wire

    def send_game_packet(self, cmd, body_hex="", result=None) -> bytes:
        """发送一条游戏封包 (自动用当前密钥加密, 计算序列号), 返回 wire 封包.

        cmd: 命令号; body_hex: 包体十六进制; result: 可覆盖序列号 (默认按 MSerial 计算).
        """
        body = hex_to_bytearray(body_hex or "")
        if result is None:
            self.last_result = compute_result(self.last_result, body, f"{int(cmd):08x}") & 0xFFFFFFFF
            result = self.last_result
        plain_len = 17 + len(body)
        payload = (struct.pack(">I", plain_len)
                   + bytes([0x31]) + struct.pack(">I", int(cmd))
                   + struct.pack(">I", int(self.account)) + struct.pack(">I", result & 0xFFFFFFFF)
                   + bytes(body))
        self._apply_key(self.session_key or DEFAULT_KEY)
        wire = encrypt(payload)
        self.tcp.send(wire)
        self._emit_frame("SEND", wire.hex(), int(cmd), bytes(body).hex())
        return wire

    def recv_packets(self, count=1, timeout=3):
        """接收并解析 count 条服务器封包 (解密), 返回列表."""
        out = []
        for _ in range(count):
            try:
                r = self.recv_game_packet(timeout=timeout)
                if r:
                    out.append(r)
            except WebSocketTimeout:
                break
            except WebSocketClosed:
                break
        return out

    def recv_until(self, target_cmd, max_packets=64, timeout=6):
        """读取封包, 直到读到 cmd==target_cmd 的**真正应答** (或达到 max_packets / 超时).

        登录后服务器会先推送一批 S2C NOTE 封包 (ver=0x01, 如 ENTER_MAP/GET_PET_INFO/
        NOTE_READY_TO_FIGHT/数值刷新等), 然后再回复你本次下发的命令。普通 recv_packets
        读到的往往只是那批推送, 而真正回应在它们之后。本函数利用 ver 字节区分:
        - ``kind=='note'``(0x01) 的包视为服务器**主动推送**, 即使 cmd==target_cmd 也不停下;
        - 只有看到 ``kind!='note'`` 且 cmd==target_cmd 的包 (即 0x3E 应答 / 其它) 才判定为
          本次命令的应答并停止。
        期间若收到时间同步(0x3EA)会顺手回 time_check 以维持连接。

        返回读到的全部封包列表 (含 target_cmd 的应答, 若找到).
        """
        import time as _time
        out = []
        start = _time.time()
        try:
            while len(out) < max_packets:
                remaining = timeout - (_time.time() - start)
                if remaining <= 0:
                    break
                try:
                    r = self.recv_game_packet(timeout=max(0.2, min(1.0, remaining)))
                except WebSocketTimeout:
                    break
                except WebSocketClosed:
                    break
                out.append(r)
                if r["cmd"] == 0x3EA:                 # 服务器时间同步 -> 回应, 维持连接
                    try:
                        self.send_time_check(bytes(r["body"]).hex())
                    except Exception:
                        pass
                if r["cmd"] == target_cmd and r.get("kind") != "note":   # 真正应答才停
                    break
        except Exception:
            pass
        return out

    def recv_game_packet(self, timeout=None):
        """读取服务器一条加密封包, 解密并解析为 PacketData.

        ver 字节按 _ver_kind 判定包类型 (note=主动推送 / response=对请求的应答),
        并入返回值 ``kind`` 字段; 客户端(登录等)可用它决定"哪些是真正的应答".
        """
        wire = self.tcp.read_packet(timeout)
        self._apply_key(self.session_key or DEFAULT_KEY)   # 登录(1001)后会自动切换为会话密钥
        dec = decrypt(wire)                      # [4B plainLen][ver][cmd][uid][res][body]
        plain = bytes(dec[4:])
        if len(plain) < 13:
            raise ValueError("解密后的封包过短")
        ver = plain[0]
        cmd = int.from_bytes(plain[1:5], "big")
        uid = int.from_bytes(plain[5:9], "big")
        res = int.from_bytes(plain[9:13], "big")
        body = plain[13:]
        kind = _ver_kind(ver)                    # 由 ver 字节判定类型
        self.received_cmds.append(cmd)
        self._emit_frame("RECV", wire.hex(), cmd, body.hex())
        if cmd == CMD_LOGIN:
            self.is_logged_in = True
            self.last_result = res & 0xFFFFFFFF
        return {"ver": ver, "cmd": cmd, "uid": uid, "result": res, "body": body,
                "kind": kind}

    def login_game(self, host=None, port=None, max_seconds=10, on_packet=None,
                   verbose=False):
        """一步完成游戏服务器登录: 连接 -> 发登录 -> 读角色数据.

        返回 (conn_str, responses); responses 为解密后的封包列表.
        """
        import time as _time
        conn = self.connect_game(host, port)
        bytes_sess = self.session
        self.send_game_login(bytes_sess)
        if verbose:
            print(f"  -> 已发送游戏登录 (cmd=1001), 等待服务器响应...", flush=True)
        responses = []
        start = _time.time()
        try:
            while _time.time() - start < max_seconds:
                try:
                    r = self.recv_game_packet(timeout=max(0.2, min(2, max_seconds - (_time.time() - start))))
                except WebSocketTimeout:
                    continue
                responses.append(r)
                if verbose:
                    body_txt = bytes(r["body"]).hex()[:48]
                    print(f"  <- cmd={r['cmd']} len={len(r['body'])} body={body_txt}", flush=True)
                if on_packet:
                    on_packet(r)
                if r["cmd"] == CMD_LOGIN and len(r["body"]) > 100:
                    # 已拿到角色数据; 按帖子规则派生会话密钥, 之后所有封包用它解密
                    sk = self.derive_session_key(r["body"])
                    if verbose:
                        print(f"  ... 已派生会话密钥: {sk}", flush=True)
                    break
        except WebSocketClosed:
            if verbose:
                print("  服务器关闭了连接", flush=True)
        finally:
            # 保留连接 (stop 由调用方决定)
            pass
        return conn, responses

    # ---- 保持会话存活 ----
    def hold(self, interval: float = 5.0, max_seconds: float = 0,
             on_heartbeat=None, on_packet=None, verbose: bool = False):
        """登录成功后保持 WebSocket 存活并持续心跳, 直到被中断或到达 max_seconds.

        - 每 interval 秒发送一次心跳 (cmd 0x3EA)
        - 循环读取服务器封包: 收到 cmd 0x3EA (时间同步) 时回 SYSTEM_TIME_CHECK
        - 收到 WebSocket 关闭帧 / 连接断开时安全退出
        - max_seconds=0 表示一直保持, 直到 Ctrl+C (KeyboardInterrupt)

        返回 True 表示按预期结束; 返回 False 表示连接异常断开.
        """
        import time as _time
        start = _time.time()
        ok = True
        try:
            while True:
                if max_seconds and (_time.time() - start) >= max_seconds:
                    break
                self.send_heartbeat()
                if on_heartbeat:
                    on_heartbeat()

                deadline = _time.time() + interval
                while _time.time() < deadline:
                    timeout = max(0.1, deadline - _time.time())
                    try:
                        pkt = self.recv_packet(timeout=timeout)
                    except WebSocketClosed:
                        print("\n服务器关闭了连接")
                        ok = False
                        return ok
                    if pkt is None:
                        continue
                    cmd = int(pkt.cmd_id, 16)
                    if verbose:
                        print(f"  <- 收到 cmd={cmd} body={pkt.body[:32]}", flush=True)
                    if cmd == 0x3EA:  # 服务器时间同步请求
                        self.send_time_check(pkt.body)
                        if verbose:
                            print(f"  -> 回 SYSTEM_TIME_CHECK", flush=True)
                    if on_packet:
                        on_packet(cmd, pkt)
        except KeyboardInterrupt:
            print("\n保持连接已中断 (Ctrl+C)")
        finally:
            self.close()
        return ok

    # ---- 静默监听 (不发心跳) ----
    def listen(self, max_seconds: float = 30.0, on_packet=None, verbose: bool = False):
        """登录成功后**不发心跳**, 只静默接收服务器主动推送, 用于观察进游戏数据.

        - max_seconds 秒后结束 (0 = 直到 Ctrl+C)
        - 收到 cmd 0x3EA 时仍回 SYSTEM_TIME_CHECK (回复后才不会断开), 但其余只收不发
        - 所有收到的封包都会走 on_frame / on_packet 回调, 便于 --log-file 记录
        """
        import time as _time
        start = _time.time()
        ok = True
        try:
            while True:
                if max_seconds and (_time.time() - start) >= max_seconds:
                    break
                remaining = (max_seconds - (_time.time() - start)) if max_seconds else 2.0
                timeout = 0.5 if remaining <= 0.5 else min(2.0, remaining)
                try:
                    pkt = self.recv_packet(timeout=timeout)
                except WebSocketClosed:
                    print("\n服务器关闭了连接")
                    ok = False
                    break
                if pkt is None:
                    continue
                cmd = int(pkt.cmd_id, 16)
                if verbose:
                    print(f"  <- 收到 cmd={cmd} body={pkt.body[:64]}", flush=True)
                if cmd == 0x3EA:  # 时间同步请求, 回复以免被断开
                    self.send_time_check(pkt.body)
                    if verbose:
                        print(f"  -> 回 SYSTEM_TIME_CHECK", flush=True)
                if on_packet:
                    on_packet(cmd, pkt)
        except KeyboardInterrupt:
            print("\n监听已中断 (Ctrl+C)")
        finally:
            self.close()
        return ok

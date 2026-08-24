#!/usr/bin/env python3
"""极简 mock 游戏服务器, 用于本地验证 WebUI 登录+发包流程 (纯 socket 实现).

用法: python3 mock_server.py --port 12001
行为:
  - 收到 cmd=1001(登录) 时, 回一条长度>100 的 LOGIN_IN 响应(末4字节做会话密钥种子)。
  - 之后收到的每条封包都解密, 并回一条短应答(便于 WebUI 的 recv_packets 读返回)。
  - 所有解密出的封包追加到内存, 支持 GET /received 查询(JSON)。
"""
import argparse
import hashlib
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from seer.algorithm import Decrypt
from seer.client import DEFAULT_KEY, CMD_LOGIN, compute_result
from seer.packet import encrypt as packet_encrypt

KEY_DEFAULT = DEFAULT_KEY.encode("utf-8")


def set_key(k):
    """切换 seer 算法库的全局密钥 (与客户端 _apply_key 一致)."""
    from seer import algorithm
    algorithm._key = k.encode("utf-8") if isinstance(k, str) else bytes(k)


class Srv:
    def __init__(self):
        self.received = []
        self.session_key = None


srv = Srv()


def handle_conn(conn):
    conn.settimeout(15)
    try:
        # ---- 1. 读登录封包(默认密钥) ----
        set_key(KEY_DEFAULT)
        wire = read_wire(conn)
        pkt = parse_wire(wire)
        uid = pkt["uid"]
        srv.received.append({"dir": "RECV", "cmd": pkt["cmd"], "uid": uid,
                             "result": pkt["result"], "body": pkt["body"].hex()})
        if pkt["cmd"] == CMD_LOGIN:
            body = bytes(range(1, 121))            # 120B >100, 末4字节=种子
            res = compute_result(0, body, f"{CMD_LOGIN:08x}") & 0xFFFFFFFF
            srv.session_key = derive_seed_key(body, uid)
            send_pkt(conn, CMD_LOGIN, uid, res, body, KEY_DEFAULT)
            print(f"[mock] login ok uid={uid} seed={body[-4:].hex()} sk={srv.session_key}", flush=True)
            # ---- 之后用会话密钥解密 ----
            while True:
                set_key(srv.session_key)
                wire = read_wire(conn)
                pkt = parse_wire(wire)
                srv.received.append({"dir": "RECV", "cmd": pkt["cmd"], "uid": pkt["uid"],
                                     "result": pkt["result"], "body": pkt["body"].hex()})
                send_pkt(conn, pkt["cmd"], pkt["uid"], 0, ack_for(pkt["cmd"]), srv.session_key)
    except Exception as e:
        print(f"[mock] conn end: {e}", flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def read_wire(conn):
    """读一条 wire 封包: [4B 长度(含本前缀)][cipher]."""
    hdr = _recv_exact(conn, 4)
    ln = struct.unpack(">I", hdr)[0]
    cipher = _recv_exact(conn, ln - 4)   # ln 是整条 wire 长度(含 4 字节前缀)
    return hdr + cipher


def _pet_front(idv, name, level, hp, atk, df, sa, sd, sp):
    n = name.encode("utf-8").ljust(16, b"\x00")
    vals = [1, 31, 5, 1, level, 1000, 100, 200, hp, hp + 50, atk, df, sa, sd, sp,
            0, 10, 20, 30, 40, 50, 0]
    return struct.pack(">I", idv) + n + b"".join(struct.pack(">I", v) for v in vals)


def _full_pet(idv, name, level, hp, atk, df, sa, sd, sp):
    """构造一只完整 PetInfo(含 5 技能/1 特性/抗性/能力值/curHp)."""
    front = _pet_front(idv, name, level, hp, atk, df, sa, sd, sp)
    skills = b"".join(struct.pack(">I", 100 + i) + struct.pack(">I", 3) for i in range(5))
    post = b"".join(struct.pack(">I", v) for v in [12345, 1, 2, 3, 7, 8, 9, 10])
    # 一个效果 (24B): itemId(u32)+status(u8)+leftCount(u8)+effectID(u16)+8×[a(u8)+extra(u8)]
    effect = struct.pack(">IBBH", 10, 2, 1, 171) + b"".join(struct.pack(">BB", v & 0xff, 0) for v in [1, 2, 3, 4, 5, 6, 0, 0])
    # 抗性 (56B): 3×(cirt/regular/precent) + 3×ctl + 3×weak + 5×u32
    def c(n, a): return struct.pack(">I", (n << 16) | (a & 0xffff))
    def t(i, v, a): return struct.pack(">I", (i << 16) | (v << 8) | (a & 0xff))
    resist = c(5, 2) + c(6, 1) + c(7, 0)
    for i in range(3): resist += t(i + 1, 1, 2)
    for i in range(3): resist += t(i + 1, 3, 4)
    resist += b"".join(struct.pack(">I", v) for v in [0, 1, 0, 0, 0])
    tail = struct.pack(">II", 0, 0) + b"".join(struct.pack(">I", v) for v in [0x00010002, 0x00030004, 0x00050006])
    for nm in [hp, atk, df, sa, sd, sp]:
        tail += struct.pack(">III", nm, nm, nm)      # base/pvp/pve
    tail += struct.pack(">III", hp, hp, hp)          # curHp×3
    return front + skills + post + struct.pack(">H", 1) + effect + resist + tail


def ack_for(cmd):
    """为指定命令返回一个应答体 (43706/2301 返回含能力值的完整 PetInfo; 其余返回占位)."""
    if cmd == 2361:
        # 爱宠/精英仓库: [count][{id,isBright,catchTime} × count]  (12B/条 PetListInfo)
        pets = [(466, 0, 777777), (901, 1, 888888)]
        s = struct.pack(">I", len(pets))
        for pid, bright, ct in pets:
            s += struct.pack(">III", pid, bright, ct)
        return s
    if cmd == 9015:
        # 精英仓库: [count][{flag,capTm,petId,raw,trainSkip} × count]  (20B/条)
        pets = [(466, 777777, 36000), (901, 888888, 0)]
        s = struct.pack(">I", len(pets))
        for pid, ct, raw in pets:
            s += struct.pack(">IIIII", 1, ct, pid, raw, 0)   # _flag,_capTm,_petId,_loc3_,_skip
        return s
    if cmd == 2303:
        # 仓库列表: [count][{id, isBright, catchTime, level} × count]  (16B/条)
        pets = [(500, 777, 60), (466, 888, 90), (900, 999, 70), (466, 1000, 50)]
        s = struct.pack(">I", len(pets))
        for pid, ct, lv in pets:
            s += struct.pack(">IIII", pid, 0, ct, lv)
        return s
    if cmd == 41921:
        return _teams_41921_response(1, 466, 467)
    if cmd == 41922:
        return b"\x00\x00\x00\x01"
    if cmd == 43706:
        p1 = _full_pet(466, "雷伊", 100, 350, 360, 300, 340, 310, 380)
        p2 = _full_pet(467, "盖亚", 90, 330, 350, 290, 320, 300, 370)
        p3 = _full_pet(468, "卡修斯", 80, 300, 340, 280, 310, 290, 350)
        return struct.pack(">I", 2) + p1 + p2 + struct.pack(">I", 1) + p3
    if cmd == 2301:
        return _full_pet(466, "雷伊", 100, 350, 360, 300, 340, 310, 380)
    return b"\x00\x00\x00\x01"


def _utf16(s):
    return s.encode("utf-8")[:64].ljust(64, b"\x00")


def _teams_41921_response(cur_id, pet1_catch, pet2_catch):
    """构造 41921 阵容列表响应 (2 套阵容)."""
    def team(tid, nick, catch1, catch2):
        b = struct.pack(">I", tid)
        b += _utf16(nick)
        # 12 格精灵 [catchtime, subhpflag]
        slots = [[catch1, 0], [catch2, 0]] + [[0, 0]] * 10
        for ct, sf in slots:
            b += struct.pack(">II", ct, sf)
        for j in range(5):
            b += struct.pack(">I", 10 + j)          # 装备
        b += struct.pack(">I", 100)                 # title
        b += _utf16("key.lineup")[:128].ljust(128, b"\x00")
        b += _utf16("key.create")[:128].ljust(128, b"\x00")
        b += struct.pack(">I", 111)                 # share_time
        b += struct.pack(">I", 222)                 # create_time
        return b
    t1 = team(1, "移动巅峰队", pet1_catch, pet2_catch)
    t2 = team(2, "备用队伍", pet2_catch, 0)
    return struct.pack(">I", cur_id) + struct.pack(">I", 2) + t1 + t2


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        ch = conn.recv(n - len(buf))
        if not ch:
            raise EOFError("connection closed")
        buf += ch
    return buf


def send_pkt(conn, cmd, uid, result, body, key):
    set_key(key)
    plain_len = 17 + len(body)
    payload = (struct.pack(">I", plain_len) + bytes([0x31]) + struct.pack(">I", cmd)
               + struct.pack(">I", uid) + struct.pack(">I", result & 0xFFFFFFFF) + body)
    conn.sendall(packet_encrypt(payload))


def parse_wire(wire):
    set_key(srv.session_key or KEY_DEFAULT)
    plain = bytes(Decrypt(wire[4:]))
    return {"ver": plain[0], "cmd": int.from_bytes(plain[1:5], "big"),
            "uid": int.from_bytes(plain[5:9], "big"),
            "result": int.from_bytes(plain[9:13], "big"), "body": plain[13:]}


def derive_seed_key(body, uid):
    seed = int.from_bytes(bytes(body)[-4:], "big")
    xorv = seed ^ (uid & 0xFFFFFFFF)
    return hashlib.md5(str(xorv).encode("utf-8")).hexdigest()[:10]


class HTTPH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/received":
            data = json.dumps({"received": srv.received, "session_key": srv.session_key}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12001)
    ap.add_argument("--http", type=int, default=12002)
    a = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", a.port))
    s.listen(16)
    print(f"[mock] game TCP on 127.0.0.1:{a.port}", flush=True)

    def accept_loop():
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()
    threading.Thread(target=accept_loop, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", a.http), HTTPH)
    httpd.allow_reuse_address = True
    print(f"[mock] status HTTP on 127.0.0.1:{a.http}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

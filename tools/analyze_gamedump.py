#!/usr/bin/env python3
"""analyze_gamedump.py — 赛尔号游戏本体抓包离线分析器（复原抓包分析能力）

从一份 TCP 抓包记录（形如 `refs/gamedump4.txt`，UTF-16LE 或 UTF-8，字段
`<序号>\\t<时间>\\t<srcIP>\\t<dstIP>\\t<srcPort>\\t<dstPort>\\t<hex载荷>`）
还原赛尔号游戏服务器的 TCP 流：

  1. 按连接(5 元组)分组，重组每个方向的字节流（处理抓包工具把同一份数据在
     不同记录间重复/覆盖捕获的情况）。
  2. 用 4 字节大端长度前缀切出封包（wire = [4B总长][密文]）。
  3. 用 seer 算法解密。登录(1001)之前用默认密钥 `!crAckmE4nOthIng:-)`，
     之后按参考帖规则从 LOGIN_IN 响应派生 **会话密钥** 并切换。
  4. 还原 cmd / uid / res / body，并用 `cmdmap.json` 映射命令名。

用到的算法/结构均来自本仓库的 `seer/` 与 `refs/seerpacket/`（与 52pojie
thread-1468888 / thread-2053139 的逆向结论一致），仅用 Python 标准库。

用法：
    python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt
    python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt --out-decoded /tmp/d.txt --out-named /tmp/n.txt
    python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt --out-cmds /tmp/c.txt   # 命令号|包体|十进制数组
    python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt --show-c2s 8 --show-s2c 10
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

# 让本脚本能直接 import 项目根下的 seer 包（本文件位于 <根>/tools/ 下）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from seer.algorithm import Decrypt, KEY_STRING  # noqa: E402
import seer.algorithm as _alg  # noqa: E402  （用于切换全局解密密钥）

# 一些结构常量（与 seer/algorithm.py、refs/seerpacket/*.cs 一致）
DEFAULT_KEY = KEY_STRING
CMD_LOGIN = 0x3E9  # 1001
# 判“合法命令号”时用的上限：seer 的真实命令号大致 0..48701，
# 用错密钥会得到亿级乱码，因此用一个远高于真实命令号、远低于乱码的阈值。
CMD_MAX_VALID = 1_000_000


# ---------------------------------------------------------------------------
# 1. 读取并解析抓包
# ---------------------------------------------------------------------------
def load_dump(path):
    """读取抓包文本，自动识别 UTF-16LE/BE 或 UTF-8/ASCII。

    返回 list[(label, time, sip, dip, sport, dport, payload_hex)]。
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\xff\xfe":
        text = raw.decode("utf-16-le")
    elif raw[:2] == b"\xfe\xff":
        text = raw.decode("utf-16-be")
    else:
        # 先试 UTF-8，失败再退回 latin-1（同时兼容可能的 GBK 注释行）
        for enc in ("utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")

    records = []
    for line in text.split("\n"):
        line = line.strip("\r")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # 有些行可能没有 hex 载荷，跳过
            continue
        label, tm, sip, dip, sport, dport, payload = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
        if not sip or not dip:
            continue
        records.append((label, _to_int(tm), sip, dip, str(sport), str(dport), payload))
    return records


def _to_int(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return 0


def _is_private_ip(ip):
    """判断是否为私有/内网 IP（客户端一般在 192.168.x / 10.x / 172.16-31.x）。"""
    for prefix in ("192.168.", "10.", "127."):
        if ip.startswith(prefix):
            return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return False


def group_connections(records):
    """按 5 元组把记录分到连接里。

    返回 {(sip, sport, dip, dport): [ (time, payload_hex), ... ]}，
    且以“发起方(源)为第一元组”为基准索引；一个连接的两条方向会出现在
    两个 key 下（互为镜像），由调用方按方向取用。
    """
    conns = {}
    for label, tm, sip, dip, sport, dport, payload in records:
        key = (sip, sport, dip, dport)
        conns.setdefault(key, []).append((tm, payload))
    return conns


# ---------------------------------------------------------------------------
# 2. 方向重组 + 切帧
# ---------------------------------------------------------------------------
def _reassemble_one_direction(entries):
    """把某一方向的 (time, payload_hex) 列表重组成“干净的”字节流，并记录每帧开始的时间。

    抓包工具常在多个记录里重复/覆盖记录同一段数据（例如大封包既按段记录、
    又整体记录一次；或同一帧在 `recv`/更高层各记一次）。这里用“若下一条记录的
    字节以本条记录为前缀，则本条是冗余覆盖”的规则去重，再按时间顺序拼接。
    返回 (bytearray(stream), list[(frame_start_ts, frame_len)]) 供切帧用。
    """
    order = sorted(entries, key=lambda e: e[0])
    bufs = []
    for tm, payload in order:
        try:
            b = bytes.fromhex("".join(c for c in payload if c in "0123456789abcdefABCDEF"))
        except ValueError:
            continue
        bufs.append((tm, b))

    # 去重：把“是下一条记录前缀”的冗余记录去掉（覆盖捕获）。
    keep = []
    n = len(bufs)
    for i in range(n):
        tm, b = bufs[i]
        if i + 1 < n and bufs[i + 1][1].startswith(b):
            continue
        keep.append((tm, b))

    stream = bytearray()
    ts_bounds = []  # (ts, start_off, length) 记录每段来源，用于给帧配时间戳
    off = 0
    for tm, b in keep:
        stream += b
        ts_bounds.append((tm, off, len(b)))
        off += len(b)
    return stream, ts_bounds


def _frames_at(ts_bounds, pos):
    for tm, o, ln in ts_bounds:
        if o <= pos < o + ln:
            return tm
    return ts_bounds[-1][0] if ts_bounds else 0


def parse_frames(stream, ts_bounds):
    """用 4 字节大端长度前缀切帧，返回 [(start_ts, frame_bytes)]。"""
    frames = []
    i = 0
    n = len(stream)
    while i + 4 <= n:
        total = int.from_bytes(stream[i:i + 4], "big")
        if total < 5 or total > 0x100000:  # 非法长度：可能是错位/噪声，跳过 1 字节
            i += 1
            continue
        if i + total > n:
            break  # 末尾不完整
        frames.append((_frames_at(ts_bounds, i), bytes(stream[i:i + total])))
        i += total
    return frames


# ---------------------------------------------------------------------------
# 3. 解密 / 会话密钥
# ---------------------------------------------------------------------------
def decrypt_frame(frame, key):
    """解密一条 wire 封包，返回明文 [ver][cmd(4)][uid(4)][res(4)][body...] 或 None。"""
    if len(frame) < 5:
        return None
    # seer 算法的解密是使用模块级全局密钥，需在解密前切换。
    _alg._key = (key if isinstance(key, bytes) else str(key).encode("utf-8"))
    try:
        plain = Decrypt(frame[4:])
    except Exception:
        return None
    if len(plain) < 13:
        return None
    return plain


def parse_plain(plain):
    ver = plain[0]
    cmd = int.from_bytes(plain[1:5], "big")
    uid = int.from_bytes(plain[5:9], "big")
    res = int.from_bytes(plain[9:13], "big")
    body = plain[13:]
    return {"ver": ver, "cmd": cmd, "uid": uid, "res": res, "body": body}


def is_sane_cmd(cmd):
    return cmd is not None and 0 <= cmd <= CMD_MAX_VALID


def derive_session_key(login_res_body, uid):
    """从 LOGIN_IN(1001) 响应正文派生会话密钥。

    规则（参考帖/`seer/client.py::derive_session_key`）：
        seed = 响应明文最后 4 字节(大端 uint)
        xor  = seed ^ 米米号
        key  = md5(str(xor)).hexdigest()[:10]
    """
    body = bytes(login_res_body)
    seed = int.from_bytes(body[-4:], "big") if len(body) >= 4 else 0
    xorv = seed ^ (int(uid) & 0xFFFFFFFF)
    import hashlib
    return hashlib.md5(str(xorv).encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# 4. 主分析
# ---------------------------------------------------------------------------
def is_game_conn(frame_lists):
    """一个连接是否承载赛尔号游戏封包：任一方向存在用默认密钥能解出 cmd==1001 的帧。"""
    for frames in frame_lists:
        for ts, fr in frames:
            plain = decrypt_frame(fr, DEFAULT_KEY)
            if plain and int.from_bytes(plain[1:5], "big") == CMD_LOGIN:
                return True
    return False


def run_full(records):
    """主流程：识别游戏连接 -> 派生会话密钥 -> 逐帧解密-> 输出结构化结果。"""
    conns = group_connections(records)

    # 收集每个连接的两条方向的帧
    bag = {}
    for key in conns:
        sip, sport, dip, dport = key
        dirs = []
        for direction, ent in [((sip, sport, dip, dport), conns.get((sip, sport, dip, dport))),
                               ((dip, dport, sip, sport), conns.get((dip, dport, sip, sport)))]:
            if not ent:
                continue
            stream, ts_bounds = _reassemble_one_direction(ent)
            frames = parse_frames(stream, ts_bounds)
            dirs.append((direction, frames, stream))
        bag[key] = dirs

    # 找游戏连接：有 1001 登录帧的那个
    game_keys = []
    for key, dirs in bag.items():
        if any(frames for _d, frames, _s in dirs):
            if is_game_conn([frames for _d, frames, _s in dirs]):
                game_keys.append(key)

    if not game_keys:
        return {"error": "未找到承载赛尔号游戏封包(含 cmd=1001 登录帧)的连接", "bags": bag}

    # 取幅最大 / 最可能是主连接的游戏连接
    def score(key):
        return sum(len(f) for _d, f, _s in bag[key])
    game_key = max(game_keys, key=score)

    # 该连接两条方向。用 1001 登录帧的“体积”区分方向：
    #   - 客户端发出的登录帧包体很小（session(16) + 固定尾，约 124 字节）→ 该方向为 C2S；
    #   - 服务器回的角色数据 1001 很大（约 7KB）→ 该方向为 S2C。
    SIZEOF_SMALL_LOGIN = 256  # 客户端登录体远小于此，服务器角色数据远大于此
    c2s_dir = None
    s2c_dir = None
    c2s_login_ts = None
    s2c_login_ts = None
    for direction, frames, stream in bag[game_key]:
        # 客户端登录帧（cmd==1001 且包体较小）
        small_login = None
        # 服务器登录响应帧（cmd==1001 且包体较大，角色数据）
        big_login = None
        for ts, fr in frames:
            plain = decrypt_frame(fr, DEFAULT_KEY)
            if plain is None:
                continue
            if int.from_bytes(plain[1:5], "big") != CMD_LOGIN:
                continue
            body = plain[13:]
            if len(body) <= SIZEOF_SMALL_LOGIN:
                if small_login is None:
                    small_login = (ts, plain)
            else:
                if big_login is None:
                    big_login = (ts, plain)
        if big_login and s2c_dir is None:
            s2c_dir = (direction, frames, stream)
            s2c_login_ts = big_login[0]
        elif small_login and c2s_dir is None:
            c2s_dir = (direction, frames, stream)
            c2s_login_ts = small_login[0]

    # 兜底：若其中一边没匹配到，用“客户端 IP 为私有地址”的方向作为 C2S。
    if c2s_dir is None or s2c_dir is None:
        for direction, frames, stream in bag[game_key]:
            if c2s_dir is None and _is_private_ip(direction[0]):
                c2s_dir = (direction, frames, stream)
            elif s2c_dir is None and not _is_private_ip(direction[0]):
                s2c_dir = (direction, frames, stream)

    if c2s_dir is None or s2c_dir is None:
        return {"error": "未能同时识别出登录(1001)的出口/入站方向", "game_key": game_key, "bag": bag}

    # 派生会话密钥（用 S2C 的登录响应）
    uid = None
    login_res_body = None
    login_res_ts = 0
    for ts, fr in s2c_dir[1]:
        plain = decrypt_frame(fr, DEFAULT_KEY)
        if plain and int.from_bytes(plain[1:5], "big") == CMD_LOGIN and len(plain[13:]) > SIZEOF_SMALL_LOGIN:
            uid = int.from_bytes(plain[5:9], "big")
            login_res_body = plain[13:]
            login_res_ts = ts
            break
    session_key = derive_session_key(login_res_body, uid) if login_res_body is not None else None

    # 逐帧解密（先默认密钥，后会话密钥）
    def decode_frames(dir_frames, kind):
        out = []
        noise = 0
        for ts, fr in dir_frames:
            info = None
            chosen = None
            for key in (DEFAULT_KEY, session_key):
                if key is None:
                    continue
                plain = decrypt_frame(fr, key)
                if plain is None:
                    continue
                p = parse_plain(plain)
                if is_sane_cmd(p["cmd"]):
                    info = p
                    chosen = key
                    break
            if info is None:
                # 用哪个密钥都解不出合理命令号：通常是抓包工具对同一帧的覆盖捕获(冗余)，
                # 记为噪声，不进入结果。
                noise += 1
                continue
            info["ts"] = ts
            info["dir"] = kind
            info["wire"] = fr
            info["key"] = chosen
            out.append(info)
        return out, noise

    c2s_dec, c2s_noise = decode_frames(c2s_dir[1], "C2S")
    s2c_dec, s2c_noise = decode_frames(s2c_dir[1], "S2C")

    return {
        "game_key": game_key,
        "c2s_dir": c2s_dir[0],
        "s2c_dir": s2c_dir[0],
        "uid": uid,
        "session_key": session_key,
        "login_res_ts": login_res_ts,
        "c2s_decoded": c2s_dec,
        "s2c_decoded": s2c_dec,
        "c2s_total_frames": len(c2s_dir[1]),
        "s2c_total_frames": len(s2c_dir[1]),
        "c2s_noise": c2s_noise,
        "s2c_noise": s2c_noise,
    }


# ---------------------------------------------------------------------------
# 5. 输出
# ---------------------------------------------------------------------------
def load_cmdmap(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        return {int(k): v for k, v in m.items() if str(k).isdigit()}
    return {}


def _ascii_of(body):
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in body)


def format_decoded(dec, cmdmap):
    lines = []
    for i, p in enumerate(dec):
        body = p["body"]
        name = cmdmap.get(p["cmd"], "----")
        lines.append(
            "  [{i}] cmd={cmd} {name} uid={uid} res=0x{res:08x} body({blen})={hex} ascii='{ascii}'".format(
                i=i,
                cmd=p["cmd"],
                name="%-28s" % name,
                uid=p["uid"],
                res=p["res"],
                blen=len(body),
                hex=body[:48].hex() if body else "",
                ascii=_ascii_of(body[:48]),
            )
        )
    return lines


def format_named(dec, cmdmap):
    from collections import Counter
    cnt = Counter(p["cmd"] for p in dec)
    lines = []
    for cmd, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])):
        name = cmdmap.get(cmd, "----")
        lines.append("  cmd= %5d x%-5d %s" % (cmd, n, name))
    return lines


def format_cmd_decimal(dec, cmdmap):
    """按 README 的封包结构，把每条命令输出为
    `cmd=<命令号> 名称 body(<长度>)=<包体hex> ints=<十进制数组(4字节大端 int32)>`。

    十进制数组用 `seer/body.py::decode_body` 把标准包体按 4 字节大端 int32 切分，
    与 WebUI「服务器响应」表的十进制数组列一致。
    """
    from seer.body import decode_body
    lines = []
    for i, p in enumerate(dec):
        body = p["body"]
        name = cmdmap.get(p["cmd"], "----")
        db = decode_body(body, signed=True)
        ints = db["ints"]
        rem = db["remainder"]
        ints_s = "[" + ", ".join(str(x) for x in ints) + "]"
        line = "[{i}] cmd={cmd} {name} body({blen})={hex} ints={ints}".format(
            i=i, cmd=p["cmd"], name="%-28s" % name, blen=len(body),
            hex=body.hex() if body else "", ints=ints_s)
        if rem:
            line += "  [非对齐尾部:%s]" % rem
        lines.append(line)
    return lines


def write_cmds_csv(dec_c2s, dec_s2c, cmdmap, path,
                   int_preview=512, hex_preview=4096):
    """查询用的 CSV：一行为一条命令。

    列: direction(方向) index(方向内序号) cmd(命令号) cmd_name(命令名)
        uid(米米号) result(序列号) body_len(包体长度) body_hex(包体hex)
        ints(包体十进制数组, 4字节大端 int32) remainder(非对齐尾字节hex)。

    为便于 Excel 直接打开/查阅，超长字段(超大包体)会被**截断预览**：
      - body_hex 最多保留 hex_preview 个字符；
      - ints 最多保留 int_preview 个 int32 值；
    截断时在末尾追加 "...(共 M 字节 / 共 N 个值)"，完整数据仍可从
    gamedump4_decoded.txt 查看或重新生成。
    """
    from seer.body import decode_body
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:  # BOM 便于 Excel 直接打开
        w = csv.writer(fh)
        w.writerow(["direction", "index", "cmd", "cmd_name", "uid", "result",
                    "body_len", "body_hex", "ints", "remainder"])
        for kind, dec in (("C2S", dec_c2s), ("S2C", dec_s2c)):
            for i, p in enumerate(dec):
                body = p["body"]
                db = decode_body(body, signed=True)
                ints = db["ints"]
                total_ints = len(ints)
                shown = ints[:int_preview]
                ints_s = "[" + ", ".join(str(x) for x in shown) + "]"
                if total_ints > int_preview:
                    ints_s += " ...(共 %d 个值)" % total_ints

                hexs = body.hex()
                if len(hexs) > hex_preview:
                    hexs = hexs[:hex_preview] + " ...(共 %d 字节)" % len(body)

                w.writerow([
                    kind,                       # direction
                    i,                          # index
                    p["cmd"],                   # cmd
                    cmdmap.get(p["cmd"], "----"),  # cmd_name
                    p["uid"],                   # uid
                    p["res"],                   # result
                    len(body),                  # body_len
                    hexs,                       # body_hex
                    ints_s,                     # ints
                    db["remainder"],            # remainder
                ])


def main(argv=None):
    ap = argparse.ArgumentParser(description="赛尔号抓包离线分析器（复原抓包分析能力）")
    ap.add_argument("--dump", default="refs/gamedump4.txt", help="抓包文本路径")
    ap.add_argument("--cmdmap", default=os.path.join(_ROOT, "cmdmap.json"), help="命令名映射 json")
    ap.add_argument("--out-decoded", default=None, help="把逐条解码结果写入文件")
    ap.add_argument("--out-named", default=None, help="把命令名统计写入文件")
    ap.add_argument("--out-cmds", default=None,
                    help="把每条命令按 命令号-包体-十进制数组 写入文件(十进制数组按 4 字节大端 int32 切分)")
    ap.add_argument("--out-csv", default=None,
                    help="把每条命令写入 CSV(含 方向/序号/命令号/命令名/uid/序列号/包体hex/十进制数组), 便于人工查阅")
    ap.add_argument("--show-c2s", type=int, default=0, help="打印前 N 条 C2S 解码结果")
    ap.add_argument("--show-s2c", type=int, default=0, help="打印前 N 条 S2C 解码结果")
    args = ap.parse_args(argv)
    records = load_dump(args.dump)
    cmdmap = load_cmdmap(args.cmdmap)
    res = run_full(records)

    if "error" in res:
        print("分析失败：", res["error"])
        return 1

    print("=== 赛尔号抓包离线分析 ===")
    print("游戏连接: %s:%s <-> %s:%s" % res["game_key"])
    print("米米号(uid):", res["uid"])
    print("会话密钥(session_key):", res["session_key"])
    print("登录响应时间戳:", res["login_res_ts"])
    print("C2S 帧数=%d  解密命令数=%d  (噪声/重复截帧=%d)   S2C 帧数=%d  解密命令数=%d  (噪声/重复截帧=%d)"
          % (res["c2s_total_frames"], len(res["c2s_decoded"]), res.get("c2s_noise", 0),
             res["s2c_total_frames"], len(res["s2c_decoded"]), res.get("s2c_noise", 0)))
    print()

    c2s_lines = format_decoded(res["c2s_decoded"], cmdmap)
    s2c_lines = format_decoded(res["s2c_decoded"], cmdmap)

    print("===== CLIENT->SERVER (%d) =====" % len(c2s_lines))
    for ln in c2s_lines[: args.show_c2s if args.show_c2s else len(c2s_lines)]:
        print(ln)
    print()
    print("===== SERVER->CLIENT (%d) =====" % len(s2c_lines))
    for ln in s2c_lines[: args.show_s2c if args.show_s2c else len(s2c_lines)]:
        print(ln)
    print()

    # 命令名统计
    print("===== 客户端->服务器 命令号(去重, 次数) =====")
    for ln in format_named(res["c2s_decoded"], cmdmap):
        print(ln)
    print()
    print("===== 服务器->客户端 命令号(去重, 次数) =====")
    for ln in format_named(res["s2c_decoded"], cmdmap):
        print(ln)

    if args.out_decoded:
        with open(args.out_decoded, "w", encoding="utf-8") as fh:
            fh.write("session_key=%s\n" % res["session_key"])
            fh.write("C2S 解密命令数=%d  S2C 解密命令数=%d\n\n" %
                     (len(res["c2s_decoded"]), len(res["s2c_decoded"])))
            fh.write("===== CLIENT->SERVER =====\n")
            fh.write("\n".join(c2s_lines) + "\n\n")
            fh.write("===== SERVER->CLIENT =====\n")
            fh.write("\n".join(s2c_lines) + "\n")
        print("\n已写入:", args.out_decoded)
    if args.out_named:
        with open(args.out_named, "w", encoding="utf-8") as fh:
            fh.write("会话密钥=%s\n\n" % res["session_key"])
            fh.write("=== 客户端->服务器 命令号(去重, 次数) ===\n")
            fh.write("\n".join(format_named(res["c2s_decoded"], cmdmap)) + "\n\n")
            fh.write("=== 服务器->客户端 命令号(去重, 次数) ===\n")
            fh.write("\n".join(format_named(res["s2c_decoded"], cmdmap)) + "\n")
        print("已写入:", args.out_named)
    if args.out_cmds:
        with open(args.out_cmds, "w", encoding="utf-8") as fh:
            fh.write("会话密钥=%s\n" % res["session_key"])
            fh.write("米米号(uid)=%s\n\n" % res["uid"])
            fh.write("===== CLIENT->SERVER (%d) 命令号 | 包体 | 十进制数组 =====\n"
                     % len(res["c2s_decoded"]))
            fh.write("\n".join(format_cmd_decimal(res["c2s_decoded"], cmdmap)) + "\n\n")
            fh.write("===== SERVER->CLIENT (%d) 命令号 | 包体 | 十进制数组 =====\n"
                     % len(res["s2c_decoded"]))
            fh.write("\n".join(format_cmd_decimal(res["s2c_decoded"], cmdmap)) + "\n")
        print("已写入:", args.out_cmds)
    if args.out_csv:
        write_cmds_csv(res["c2s_decoded"], res["s2c_decoded"], cmdmap, args.out_csv)
        print("已写入:", args.out_csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""解析游戏客户端 PetInfo / PetSkillInfo / PetEffectInfo / PetResistanceInfo 二进制结构.

来源: 反编译的 PetInfo.as, PetSkillInfo.as, PetEffectInfo.as, PetResistanceInfo.as。
数据从 ByteArray(IDataInput) 读入, 全部大端。

各子对象固定字节长度:
    PetSkillInfo     = id(u32) + pp(u32)                        = 8B
    PetEffectInfo    = itemId(u32)+status(u8)+leftCount(u8)+effectID(u16)
                       + 8 × [a(u8) + checkAdd(u8)]             = 24B (固定)
    PetResistanceInfo= 3×(cirt/regular/precent u32) + 3×(ctl_n u32)
                       + 3×(weak_n u32) + 5×u32                 = 56B

PetInfo 整体(按构造函数顺序):
    前段标量(能力值等) -> 5×PetSkillInfo -> 8×u32(捕获/刻印)
    -> effectCount(u16)+N×PetEffectInfo -> PetResistanceInfo
    -> skinId,assistMoveId -> 3×u32(能力值6个16位) -> 6×{base,pvp,pve}_total
    -> 3×curHp

因为各段长度已知, parse_full 可以完整走完一只 PetInfo, 从而切割 43706 的
[第一背包数][pet1][pet2]...[第二背包数][...] 全部精灵。
"""

from __future__ import annotations


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def _u16(b, o):
    return int.from_bytes(b[o:o + 2], "big")


def _u8(b, o):
    return b[o]


def _name(b, o, n=16):
    """读 PetInfo 的固定 16 字节名字字段.

    客户端用 readUTFBytes(16) 读入, 但中文包体常为 GBK, 且后面可能有非空填充。
    顺序尝试 utf8/gbk; 若字段全为 0(服务器可能留空, 客户端再查本地 PetXMLInfo),
    返回空串, 由调用方用 pet_names 查询或显示 id。
    """
    raw = b[o:o + n]
    # 去掉尾部常见的填充字节 (00 / 空格 / FF) 与不可打印控制字节
    raw = raw.rstrip(b"\x00\x20\xff")
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            s = raw.decode(enc)
            s = "".join(ch for ch in s if ch != "\x00" and not (0x00 <= ord(ch) < 0x20))
            return s
        except Exception:
            continue
    return ""


# 本地 pet id -> 名字 表 (服务器把名字留空时, 客户端从本地 PetXMLInfo 查; 这里供外部填充)
PET_NAMES = {}


def set_pet_names(mapping):
    """设置 id->名字 查询表 (来自外部配置, 如 pet_names.json)."""
    global PET_NAMES
    PET_NAMES = {int(k): v for k, v in (mapping or {}).items()}


def merge_pet_names(mapping):
    """把映射并入 PET_NAMES (同名 id 覆盖, 其余保留). 用于在自动导出的表上叠加用户覆盖."""
    global PET_NAMES
    for k, v in (mapping or {}).items():
        PET_NAMES[int(k)] = v


def load_pet_names(path="pet_names.json"):
    """从 pet_names.json 读取 {"id": "名字"} 并写入 PET_NAMES; 无文件/出错时忽略."""
    import json as _json
    import os
    try:
        with open(path, "r", encoding="utf-8") as f:
            set_pet_names(_json.load(f))
        return True
    except (OSError, ValueError, _json.JSONDecodeError):
        return False


def resolve_name(d):
    """返回精灵名: 优先包体里的名字; 为空则查本地表; 再空则显示 (id=xxx)."""
    nm = d.get("name") or ""
    if nm:
        return nm
    nm2 = PET_NAMES.get(d.get("id"))
    if nm2:
        return nm2
    return "(id=%s)" % d.get("id")


def _getbit(value, bit):
    """位读取: 1-based 位置 bit 是 0 还是 1."""
    return (value & (1 << (bit - 1))) >> (bit - 1)


def _bitval(value, start_bit, nbits):
    """从 start_bit(1-based) 起取 nbits 位, 作为小端合成值."""
    r = 0
    for i in range(nbits):
        r += _getbit(value, start_bit + i) * (2 ** i)
    return r


# ---- 各子对象解析 (返回 (dict, new_offset)) ----

def parse_skill(b, offset=0):
    o = offset
    d = {"id": _u32(b, o), "pp": _u32(b, o + 4)}
    return d, o + 8


def parse_effect(b, offset=0):
    o = offset
    d = {}
    d["itemId"] = _u32(b, o); o += 4
    d["status"] = _u8(b, o); o += 1
    d["leftCount"] = _u8(b, o); o += 1
    d["effectID"] = _u16(b, o); o += 2
    a = []
    for _ in range(8):
        lo = _u8(b, o); o += 1
        hi = _u8(b, o); o += 1          # checkAdd 固定读 1 字节, 非0时 *256
        a.append(lo + (hi * 256 if hi else 0))
    d["args"] = " ".join(str(x) for x in a)
    d["a"] = a
    return d, o


def parse_resistance(b, offset=0):
    o = offset
    d = {}
    for k in ("cirt", "regular", "precent"):
        v = _u32(b, o); o += 4
        d[k] = _bitval(v, 17, 16)
        d[k + "_adj"] = _bitval(v, 1, 16)
    for i in (1, 2, 3):
        v = _u32(b, o); o += 4
        d["ctl_%d_idx" % i] = _bitval(v, 17, 8)
        d["ctl_%d" % i] = _bitval(v, 9, 8)
        d["ctl_%d_adj" % i] = _bitval(v, 1, 8)
    for i in (1, 2, 3):
        v = _u32(b, o); o += 4
        d["weak_%d_idx" % i] = _bitval(v, 17, 8)
        d["weak_%d" % i] = _bitval(v, 9, 8)
        d["weak_%d_adj" % i] = _bitval(v, 1, 8)
    d["resist_all"] = _u32(b, o); o += 4
    d["resist_state"] = _u32(b, o); o += 4
    d["red_gem"] = _u32(b, o); o += 4
    d["green_gem"] = _u32(b, o); o += 4
    d["reserve"] = _u32(b, o); o += 4
    return d, o


# ---- PetInfo 解析 ----

def parse_front(body, offset=0):
    """解析 PetInfo 前段(含全部能力值), 返回 (dict, new_offset).

    前段: id, name(16B), generation, dv, nature, abilityType, level, exp, lvExp,
    nextLvExp, hp, maxHp, attack, defence, s_a, s_d, speed,
    ev_hp, ev_attack, ev_defence, ev_sa, ev_sd, ev_sp, _skip。
    """
    b = bytes(body)
    o = offset
    d = {}
    d["id"] = _u32(b, o); o += 4
    d["name"] = _name(b, o, 16); o += 16
    d["generation"] = _u32(b, o); o += 4
    for k in ("dv", "nature", "abilityType", "level", "exp", "lvExp", "nextLvExp",
              "hp", "maxHp", "attack", "defence", "s_a", "s_d", "speed",
              "ev_hp", "ev_attack", "ev_defence", "ev_sa", "ev_sd", "ev_sp"):
        d[k] = _u32(b, o); o += 4
    d["_skip"] = _u32(b, o); o += 4
    return d, o


def parse_full(body, offset=0):
    """完整解析一只 PetInfo(含技能/特性/抗性/刻印/能力值/curHp), 返回 (dict, new_offset).

    返回的 offset 即下一只 PetInfo 的起始, 用于按 43706 切割批量。
    """
    b = bytes(body)
    o = offset
    d, o = parse_front(b, o)                 # 前段
    d["skills"] = []
    for _ in range(5):
        sk, o = parse_skill(b, o)            # 5 × 8B
        d["skills"].append(sk)
    for k in ("catchTime", "catchMap", "catchRect", "catchLevel",
              "abilityMark", "skillMark", "commonMark"):
        d[k] = _u32(b, o); o += 4
    d["commonMarkActived"] = _u32(b, o); o += 4
    ec = _u16(b, o); o += 2
    d["effectCount"] = ec
    d["effects"] = []
    for _ in range(ec):
        ef, o = parse_effect(b, o)           # N × 24B
        d["effects"].append(ef)
    d["resistance"], o = parse_resistance(b, o)   # 56B
    d["skinId"] = _u32(b, o); o += 4
    d["assistMoveId"] = _u32(b, o); o += 4
    d["abilityValues"] = []
    for _ in range(3):
        v = _u32(b, o); o += 4
        d["abilityValues"].append((v >> 0) & 0xffff)
        d["abilityValues"].append((v >> 16) & 0xffff)
    for nm in ("hp", "attack", "defence", "s_a", "s_d", "speed"):
        d["base_" + nm + "_total"] = _u32(b, o); o += 4
        d["pvp_" + nm + "_total"] = _u32(b, o); o += 4
        d["pve_" + nm + "_total"] = _u32(b, o); o += 4
    d["base_curHp"] = _u32(b, o); o += 4
    d["pvp_curHp"] = _u32(b, o); o += 4
    d["pve_curHp"] = _u32(b, o); o += 4
    return d, o


def split_petbag_43706(body):
    """解析 43706 响应: [n1][pet1]..[pet2...][n2][petN...].

    返回 {"first_count", "second_count", "first_bag": [..], "second_bag": [..]}.
    """
    b = bytes(body)
    o = 0
    n1 = _u32(b, o); o += 4
    first = []
    for _ in range(n1):
        d, o = parse_full(b, o)
        first.append(d)
    n2 = _u32(b, o); o += 4
    second = []
    for _ in range(n2):
        d, o = parse_full(b, o)
        second.append(d)
    return {"first_count": n1, "second_count": n2,
            "first_bag": first, "second_bag": second}


def format_pet(d):
    ev = [d.get(k, 0) for k in ("ev_attack", "ev_defence", "ev_sa", "ev_sd", "ev_sp", "ev_hp")]
    out = dict(d)
    out["name"] = resolve_name(d)     # 覆盖包体的名字(可能为空), 用 resolve
    return ("id={id} 名字={name} 等级={level} 天赋={dv} 性格={nature} "
            "体力={hp}/{maxHp} 攻击={attack} 防御={defence} 特攻={s_a} 特防={s_d} 速度={speed} "
            "学习力(攻/防/特攻/特防/速/体)={ev}").format(**out, ev=ev)

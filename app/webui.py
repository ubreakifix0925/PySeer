#!/usr/bin/env python3
"""赛尔号协议调试 WebUI (纯标准库, 无需第三方依赖).

功能:
    1. 登录操作      --account/--password 填账号密码, 一键游戏登录 (会话密钥自动派生)
    2. 日志输出      --实时 SSE 流 (登录过程/每个收发封包/命令名)
    3. 发包测试      --登录成功后, 手动构造命令号+包体并通过当前连接发送, 读取服务器响应

用法:
    python3 app/webui.py [--host 127.0.0.1] [--port 8680]
    浏览器打开 http://127.0.0.1:8680/
"""

import argparse
import html
import json
import os
import signal
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from seer.body import decode_body, pack_body, parse_parts
from seer.client import DEFAULT_GAME_SERVER, LoginError, SeerClient
from seer.fightinfo import parse_catch_pet, parse_change_pet_info, parse_fight_over, parse_fight_start_info, parse_note_ready_to_fight, parse_note_use_skill, parse_use_pet_item
from seer.petinfo import format_pet, load_pet_names, merge_pet_names, parse_front, parse_full, resolve_name, set_pet_names, split_petbag_43706

# ---- 全局状态 ----
_LOCK = threading.Lock()
_STATE = {
    "client": None,          # 当前已登录的 SeerClient 实例
    "status": "idle",        # idle | logging_in | ready | error | disconnected
    "detail": "",
    "account": "",
    "conn": "",
    "host": "",              # 最近一次登录的游戏服务器 host (供断线重连复用)
    "port": 0,               # 最近一次登录的游戏服务器 port
    "connected": False,      # 游戏 socket 是否在线 (掉线检测后置 False)
    "disconnect_kind": "",   # 最近一次掉线类型: '' | 'server'(服务器造成) | 'active'(主动中断)
}
_LOG = []                    # 结构化日志 (供 /api/log 返回); 有上限 _LOG_MAX, 超限从头部裁剪
_COND = threading.Condition()  # 通知 SSE 有新日志
_RECV_LATEST = {}            # {cmd: 最近一条 RECV 包体(hex)} 供脚本库取值
_RECV_SEQ = {}               # {cmd: 该 cmd 的 RECV 序号} 供判断"新响应"
_LOCK_RECV = threading.Lock()  # 保护 _RECV_LATEST/_RECV_SEQ

# ---- 被动掉线自愈 (后端层面) ----
# 服务器/网络造成的被动掉线: 后端隔这么多秒后**自动重连**, 让脚本无需处理(只管继续之前的工作).
# 主动中断(如"主力阵亡立刻断线")不在此列: 那是脚本主动行为, 立即重连, 不等待.
PASSIVE_RECONNECT_WAIT = 90     # 被动掉线后自动重连前的等待秒数
_passive_reconnect_lock = threading.Lock()
_passive_reconnect_pending = False   # 是否已有一个被动重连在看守(防重复)
_passive_reconnect_at = 0.0          # 最近一次被动掉线的时刻(用于计算剩余等待)

_LOG_MAX = 5000
# 源码目录 (本项目程序文件所在 app/) 与项目根目录 (其上一级)
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_SRC_DIR)
_DATA_DIR = os.path.join(_PROJ, "data")
_LOG_DIR = os.path.join(_PROJ, "webui_logs")
_CRED_FILE = os.path.join(_DATA_DIR, "webui_credentials.json")
_FILTER_FILE = os.path.join(_DATA_DIR, "webui_filter.json")
_CMDMAP_FILE = os.path.join(_SRC_DIR, "cmdmap.json")
# 后端实际监听地址写到这里, 供 PySeer 脚本运行时自动定位 (见 PySeer.discover_backend)
_ADDR_FILE = os.path.join(_DATA_DIR, "webui_addr.json")
_HEAD_DIR = os.path.join(_DATA_DIR, "head")  # 精灵头像(按物种id)目录
_EFFECT_ICON_DIR = os.path.join(_DATA_DIR, "effecticon")  # 魂印/效果图标目录
# "脚本"页左侧默认脚本存放路径: 用户把 .py 脚本放进该目录即可在页面选择运行
SCRIPTS_DIR = os.path.join(_SRC_DIR, "scripts")

# 当前正在运行的用户脚本子进程 (供"脚本"页启动/停止)
_SCRIPT_PROC = None

_SEQ = 0  # 日志单调递增序号, 前端按它去重
_FILTER_DEFAULT = {40002, 2192, 41228, 4047, 4475, 41080, 9134, 2604, 9019,
                   2101, 2004, 3405, 2601, 2002, 43321, 1002, 9908}  # 默认过滤(舍弃)的包id


def _load_filter():
    """读取保存的过滤包id名单 (webui_filter.json); 无文件时用默认名单."""
    try:
        with open(_FILTER_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            ids = d.get("ids", []) if isinstance(d, dict) else d
            return {int(x) for x in ids}
    except (OSError, json.JSONDecodeError, ValueError):
        return set(_FILTER_DEFAULT)


def _save_filter(ids):
    """把过滤包id名单写入 webui_filter.json (用户修改后持久化)."""
    try:
        with open(_FILTER_FILE, "w", encoding="utf-8") as f:
            json.dump({"ids": sorted(ids)}, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"保存过滤名单失败: {e}")
        return False


_FILTER_IDS = _load_filter()  # 运行时可改, 同时持久化到 webui_filter.json

# 背包精灵(43706)解析结果: 出战/待命 两只列表, 供"背包"分页展示
_BAG = {"first": [], "second": [], "fetched": False, "version": 0}

# 阵容列表(41921)解析结果: 供"切换阵容"弹窗
_TEAMS = {"curUsedId": 0, "teams": [], "fetched": False, "version": 0}


def _decode_utf(x):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return x.split(b"\x00")[0].decode(enc)
        except Exception:
            continue
    return ""


# 仓库精灵列表(2303 GET_PET_LIST) 解析结果, 供"精灵仓库"分页
_STORAGE = {"pets": [], "fetched": False, "version": 0}

# 精英仓库(GET_LOVE_PET_LIST 2361 爱宠/领养, 无修炼) 解析结果
_EXE = {"pets": [], "fetched": False, "version": 0}

# 单只精灵(2301 GET_PET_INFO) 缓存: catchTime -> PetInfo dict (供仓库养成信息展示)
_PET_INFO = {}

# 对战状态 (由 2503 NOTE_READY_TO_FIGHT / 2504 NOTE_START_FIGHT 解析填充, 供"对战"页展示)
_BATTLE = {
    "active": False,          # 是否进入对战 (收到 2503 置 True)
    "finished": False,        # 是否已结束 (收到 2506 FIGHT_OVER 置 True; 脚本库对战体以此终止)
    "mode": 0,                # 对战模式 (2503)
    "my": None,               # 我方当前出战精灵 ({id,petID,nick,...}) (2504)
    "other": None,            # 敌方当前出战精灵 (2504)
    "myTeam": [],             # 我方出战队伍 ({id,catchTime,hp,skills,...}) (2503)
    "otherTeam": [],          # 敌方出战队伍 (2503)
    "mySkills": [],           # 我方当前可使用的技能 id 列表
    "mySkillPP": {},          # 我方当前技能剩余 PP ({技能id: 当前pp}, 由 2505 AttackValue.skillList 同步)
    "otherSkillPP": {},       # 敌方当前技能剩余 PP
    "myId": None,             # 我方账号(米米号), 用于区分 my/other
    "lastCmd": None,          # 最近触及更新的命令 (2503/2504)
    "lastSkill": None,        # 最近一次回合技能 (2505 前导: {userID,skillID,count,actorCatchTime})
    "round": 0,               # 本场已进行的回合数 (每收到 2505 回合包 +1, 供战报"回合 N")
    "report": [],             # 战报记录 (chronological [{t,msg}])
    "_ready_sent_mode": None, # 已自动发送过 2404 的对战模式 (防重复)
    "version": 0,
}

# 本场对战收到的完整(解密后)包体暂存: [(t, direction, cmd, body_hex)], 每场结束写入日志文件
_BATTLE_PKTS = []

# ---- 线程局部"当前处理上下文"(未绑定时回落全局) ----
# 供可选扩展件在处理某请求/某连接时绑定一个上下文; 未绑定则用全局, 行为不变.
_TL = threading.local()


def _cur_ctx():
    """当前线程绑定的处理上下文 dict; 无则 None."""
    return getattr(_TL, "ctx", None)


def _b():
    """当前线程应读写的对战状态; 绑定了上下文则用其 battle, 否则全局 _BATTLE."""
    c = _cur_ctx()
    return c["battle"] if c is not None else _BATTLE


def _ctx_client():
    """当前请求应使用的客户端; 绑定了上下文则用其 client, 否则全局 _STATE["client"]."""
    c = _cur_ctx()
    return c["client"] if c is not None else _STATE["client"]

# 技能名查询 (skills.json): id -> 中文名
def _skill_name(sid):
    try:
        d = _SKILLS.get(str(sid))
        if d and d.get("name"):
            return d["name"]
    except Exception:
        pass
    return str(sid)


def _parse_love_2361(data):
    """解析 2361 GET_LOVE_PET_LIST 精英(爱宠)仓库列表: [count u32][PetListInfo × count].

    PetListInfo = id u32 + isBright u32 + catchTime u32 (12B/条, level 由单独行存储UpDate补).
    """
    b = bytes(data)
    o = 0
    n = int.from_bytes(b[0:4], "big"); o += 4
    pets = []
    for _ in range(n):
        if o + 12 > len(b):
            break
        pid = int.from_bytes(b[o:o + 4], "big"); o += 4
        bright = int.from_bytes(b[o:o + 4], "big"); o += 4
        cap = int.from_bytes(b[o:o + 4], "big"); o += 4
        pets.append({"id": pid, "isBright": bright, "catchTime": cap})
    return pets


def _parse_storage_2303(data):
    """解析 2303 仓库列表响应(分页被 on_frame 追加).

    每格 16B = PetListInfo(id u32, isBright u32, catchTime u32) + level u32.
    (来自 PetListInfo.as 与 PetManager.getStorageArgList 的 info.level=readUnsignedInt().)
    """
    b = bytes(data)
    o = 0
    n = int.from_bytes(b[0:4], "big"); o += 4
    pets = []
    for _ in range(n):
        if o + 16 > len(b):
            break
        pid = int.from_bytes(b[o:o + 4], "big"); o += 4
        bright = int.from_bytes(b[o:o + 4], "big"); o += 4
        catch = int.from_bytes(b[o:o + 4], "big"); o += 4
        lv = int.from_bytes(b[o:o + 4], "big"); o += 4
        pets.append({"id": pid, "isBright": bright, "catchTime": catch, "level": lv})
    return pets


def _parse_teams_41921(data):
    """解析 41921 阵容列表响应: [curUsedId][len][每套: id,nick(64B),12×(ct,sf),5×equip,title,key(128B)×2,share,create]."""
    b = bytes(data)
    o = 0
    cur = int.from_bytes(b[0:4], "big"); o = 4
    n = int.from_bytes(b[o:o + 4], "big"); o += 4
    teams = []
    for _ in range(n):
        t = {}
        t["id"] = int.from_bytes(b[o:o + 4], "big"); o += 4
        nick = _decode_utf(b[o:o + 64]); o += 64
        pet = []
        for _ in range(12):
            ct = int.from_bytes(b[o:o + 4], "big"); o += 4
            sf = int.from_bytes(b[o:o + 4], "big"); o += 4
            pet.append([ct, sf])
        t["pet_detail"] = pet
        equip = [int.from_bytes(b[o + 4 * j:o + 4 * j + 4], "big") for j in range(5)]; o += 20
        t["equip"] = equip
        t["title"] = int.from_bytes(b[o:o + 4], "big"); o += 4
        t["lineup_key"] = _decode_utf(b[o:o + 128]); o += 128
        t["create_key"] = _decode_utf(b[o:o + 128]); o += 128
        t["share_time"] = int.from_bytes(b[o:o + 4], "big"); o += 4
        t["create_time"] = int.from_bytes(b[o:o + 4], "big"); o += 4
        t["nick"] = nick or ("阵容" + str(t["id"]))
        teams.append(t)
    return {"curUsedId": cur, "teams": teams}


def _pet_bag_view(p):
    """为背包里的精灵补充头像 URL (data/head/<物种id>.png 存在时) 与名字."""
    out = dict(p)
    pid = out.get("id")
    fname = "%s.png" % pid if isinstance(pid, int) and pid > 0 else None
    if fname and os.path.isfile(os.path.join(_HEAD_DIR, fname)):
        out["avatar"] = "/head/%s" % fname
    else:
        out["avatar"] = None
    out["name"] = resolve_name(out)
    out["attr"] = attr_of(out.get("id"))
    return out


def _storage_view(p):
    """仓库精灵(2303)补充头像/名字/属性, 供仓库界面拖拽复制载入图片."""
    out = dict(p)
    pid = out.get("id")
    fname = "%s.png" % pid if isinstance(pid, int) and pid > 0 else None
    if fname and os.path.isfile(os.path.join(_HEAD_DIR, fname)):
        out["avatar"] = "/head/%s" % fname
    else:
        out["avatar"] = None
    out["name"] = resolve_name(out)
    out["attr"] = attr_of(out.get("id"))
    return out


def _battle_view(p):
    """给对战精灵补充头像 URL 与名字 (按物种id), 供"对战"页显示.

    统一血量字段名: 前端血条读 **maxHP**(大写). 不同来源命名字段不一
    (2504 FightPetInfo 用 maxHP; ChangePetInfo/2503 用 maxHp 或无), 这里统一成
    同时提供 maxHP 与 maxHp, 避免换宠后最大HP显示为 0.
    """
    out = dict(p)
    pid = out.get("id") or out.get("petID")
    if out.get("id") is None and pid is not None:
        out["id"] = pid        # 统一 id 字段 (2504 FightPetInfo 只给 petID), 供 resolve_name 查名
    fname = "%s.png" % pid if isinstance(pid, int) and pid > 0 else None
    out["avatar"] = ("/head/%s" % fname) if (fname and os.path.isfile(os.path.join(_HEAD_DIR, fname))) else None
    out["name"] = out.get("petName") or out.get("nick") or resolve_name(out)
    # 血量统一: maxHP = maxHp|xinMaxHp|maxHP (取任一非空), 并回填两种大小写
    mh = out.get("maxHP")
    if mh is None:
        mh = out.get("maxHp")
    if mh is None:
        mh = out.get("xinMaxHp")
    if mh is not None:
        out["maxHP"] = mh
        out["maxHp"] = mh
    return out


def _pet_state(p):
    """战报用精灵状态串: 名字(id=…) HP x/y (带阵亡标记)."""
    if not p:
        return "—"
    pid = p.get("petID") or p.get("id")
    nm = p.get("petName") or p.get("name") or (resolve_name({"id": pid}) if pid else "(未知)")
    hp = p.get("hp") if p.get("hp") is not None else p.get("xinHp")
    mh = p.get("maxHP") if p.get("maxHP") is not None else (p.get("maxHp") or p.get("xinMaxHp"))
    hp_s = "?" if hp is None else str(int(hp))
    mh_s = "?" if mh is None else str(int(mh))
    dead = " ⚠️阵亡" if (hp is not None and hp <= 0) else ""
    return f"{nm}(id={pid}) HP {hp_s}/{mh_s}{dead}"


def _update_battle(cmd, hex_body, me_id):
    """解析对战包(2503/2504)并更新当前账号对战状态(走 _b()); me_id=当前账号米米号."""
    try:
        me_id = int(me_id)          # 统一为 int, 便于与包内 id 比较
        data = bytes.fromhex(hex_body) if isinstance(hex_body, str) else bytes(hex_body)
        if cmd == 2503:
            r = parse_note_ready_to_fight(data)
            # 2503 的两个 user 按 id 匹配 me_id 决定 my/other; 找不到则 A 为我方
            a, b = r.get("userA"), r.get("userB")
            my_u = (a if a["id"] == me_id else b) if a and b else a
            ot_u = (b if a["id"] == me_id else a) if a and b else b
            with _LOCK:
                _fresh = not _b().get("active")     # 之前未在对战中 -> 本轮为全新对战开始
                _b()["mode"] = r.get("mode")
                _b()["active"] = True
                _b()["finished"] = False          # 新一轮对战开始, 清掉上一场结束标记
                _b()["lastSkill"] = None          # 清掉上一场最后一次回合数据, 防止跨场误读
                _b()["round"] = 0                 # 新一场对战, 回合计数清零
                _b()["myId"] = me_id
                _b()["myTeam"] = [_battle_view(p) for p in (my_u or {}).get("pets", [])]
                _b()["otherTeam"] = [_battle_view(p) for p in (ot_u or {}).get("pets", [])]
                _b()["mySkills"] = _active_skills(_b())
                # my/other 先取各自队伍第一只(出战首发); 后续 2504 会按需覆盖为真正当前精灵
                _b()["my"] = _battle_view(_b()["myTeam"][0]) if _b()["myTeam"] else None
                _b()["other"] = _battle_view(_b()["otherTeam"][0]) if _b()["otherTeam"] else None
                _b()["lastCmd"] = 2503
                _b()["version"] += 1
            if _fresh:
                # 后台监听到"对战开始" -> 前端自动切换到对战界面并开始监听对战流程
                log("battle", "检测到对战行为(2503 出场队伍), 已自动切换至对战界面")
            _report(f"对战开始 mode={r.get('mode')} | 我方{len(_b()['myTeam'])}只 敌方{len(_b()['otherTeam'])}只", clear=True)
            log("ok", f"对战(2503): mode={r.get('mode')} 我方{len(_b()['myTeam'])}只 敌方{len(_b()['otherTeam'])}只")
        elif cmd == 2504:
            r = parse_fight_start_info(data)
            a, b = r.get("fightPetA"), r.get("fightPetB")
            # 两个 FightPetInfo 顺序不定, 按 userID==me_id 区分我方/敌方
            if a and a.get("userID") == me_id:
                my_f, ot_f = a, b
            elif b and b.get("userID") == me_id:
                my_f, ot_f = b, a
            else:
                my_f, ot_f = a, b
            with _LOCK:
                _b()["active"] = True
                _b()["myId"] = me_id
                _b()["my"] = _battle_view(my_f) if my_f else None
                _b()["other"] = _battle_view(ot_f) if ot_f else None
                _b()["mySkills"] = _active_skills(_b())
                _b()["lastCmd"] = 2504
                _b()["version"] += 1
            _report(f"开场 [我方] {_pet_state(my_f)} | [敌方] {_pet_state(ot_f)}")
            log("ok", "对战(2504): 双方当前出战精灵已更新")
        elif cmd == 2407:
            # 换宠: 2407 CHANGE_PET 应答携带**新入场精灵**的 ChangePetInfo (完整状态).
            # 客户端发起时发 2407 + 目标精灵 catchTime (int32); 服务器据此回发新当前精灵.
            try:
                ch, _ = parse_change_pet_info(data)
                uid = ch.get("userID")
                side = "我方" if uid == me_id else ("敌方" if uid == 0 else f"?{uid}")
                pid = ch.get("petID")
                ch["id"] = pid                     # 统一 id 字段, 供名字/头像解析
                ch["petName"] = ch.get("petName") or resolve_name({"id": pid})
                ch["maxHp"] = ch.get("maxHp")
                ch["maxHP"] = ch.get("maxHp")       # 大写 maxHP, 供前端血条读取
                with _LOCK:
                    _b()["active"] = True
                    _b()["myId"] = me_id
                    pv = _battle_view(ch)
                    if uid == me_id:
                        # 我方换宠: 新当前精灵 = 该 ChangePetInfo; 从 team 里找到这只, 同步其技能/等级
                        _b()["my"] = pv
                        for p in _b().get("myTeam", []):
                            if p.get("catchTime") == ch.get("catchTime"):
                                p.update({"hp": ch.get("hp"), "maxHp": ch.get("maxHp"),
                                          "maxHP": ch.get("maxHp"),
                                          "id": pid, "level": ch.get("level"),
                                          "skills": [s[0] for s in ch.get("skillList", [])]})
                    else:
                        _b()["other"] = pv
                        for p in _b().get("otherTeam", []):
                            if p.get("catchTime") == ch.get("catchTime"):
                                p.update({"hp": ch.get("hp"), "maxHp": ch.get("maxHp"),
                                          "maxHP": ch.get("maxHp"),
                                          "id": pid, "level": ch.get("level"),
                                          "skills": [s[0] for s in ch.get("skillList", [])]})
                    _b()["mySkills"] = _active_skills(_b())
                    _b()["lastCmd"] = 2407
                    _b()["version"] += 1
                lv = ch.get("level")
                _report(f"换宠 [{side}] → {_pet_state(ch)}")
                log("ok", f"对战(2407): 换宠 [{side}] pet={pid} lv={lv} hp={ch.get('hp')}/{ch.get('maxHp')} catch={ch.get('catchTime')}")
            except Exception:
                pass
        elif cmd == 2505:
            # NOTE_USE_SKILL: 一个 2505 = 一整回合 = UseSkillInfo (firstAttackInfo + secondAttackInfo).
            # 依 refs/attack/AttackValue.as 精确解: attackBlocks 给 full AttackValue (skillID/atkTimes/
            # lostHP/gainHP/remainHP/maxHp/isCrit/state/petStatus/specailArr/...).
            try:
                sk = parse_note_use_skill(data)
                upd = sk.get("hpUpdates") or []
                blocks = sk.get("attackBlocks") or []
                with _LOCK:
                    _b()["lastSkill"] = sk
                    _b()["lastCmd"] = 2505
                    _b()["round"] = _b().get("round", 0) + 1   # 本场已进行回合数 +1
                    _apply_hp_updates(upd)
                    # AttackValue.remainHP/maxHp 是本回合**施法者**的权威血量;
                    # 按 userID 匹配到当前精灵, 避免换宠后当前血量停留在满值(未显示掉血)
                    _apply_attack_hp(blocks, me_id)
                    # AttackValue.skillList 的 [技能id, 当前pp] 是服务器权威 PP, 同步前端
                    _apply_skill_pp(blocks, me_id)
                    _b()["version"] += 1
                # —— 精简战报: 每回合只在场精灵"使用技能 + 状态" ——
                round_no = _b().get("round", 0)
                recs = blocks or (sk.get("skillRecords") or [])
                if not recs and sk.get("skillID") is not None:
                    recs = [sk]                        # 兜底: 用 2505 前导
                for r in recs:
                    rid = r.get("userID")
                    side = "我方" if rid == me_id else ("敌方" if rid == 0 else f"?{rid}")
                    pet = _b().get("my") if rid == me_id else _b().get("other")
                    nm = _pet_state(pet).split(" HP ")[0] if pet else f"id={rid}" if rid else "?"
                    atk = r.get("skillID")
                    hp, mh = r.get("remainHP"), r.get("maxHp")
                    crit = " [暴击]" if r.get("isCrit") else ""
                    dead = " ⚠️阵亡" if (hp is not None and hp == 0) else ""
                    if hp is not None:
                        mh_s = "?" if mh is None else str(int(mh))
                        _report(f"回合 {round_no} [{side}] {nm} 使用技能 {_skill_name(atk)}[{atk}] "
                                f"剩余HP {int(hp)}/{mh_s}{crit}{dead}")
                    else:
                        _report(f"回合 {round_no} [{side}] {nm} 使用技能 {_skill_name(atk)}[{atk}]{crit}{dead}")
                log("info", f"对战(2505 回合): 回合{round_no} 我方技能{sk.get('skillID')} 敌方技能"
                            f"{[r.get('skillID') for r in blocks if r.get('userID') == 0]} "
                            f"攻击块{len(blocks)} HP更新{len(upd)}")
            except Exception:
                pass
        elif cmd in (2406,):
            try:
                it, _ = parse_use_pet_item(data)
                log("info", f"对战(2406 用道具): 用户{it.get('userID')} 道具{it.get('itemID')} 改血{it.get('changeHp')}")
            except Exception:
                pass
        elif cmd in (2409,):
            try:
                cp, _ = parse_catch_pet(data)
                log("info", f"对战(2409 捕捉): 捕获 catchTime={cp.get('catchTime')} 精灵={cp.get('petID')}")
            except Exception:
                pass
        elif cmd == 2506:
            # FIGHT_OVER: 对战结束 -> 本场完整包立档 -> 输出对战结果 -> 重置对战状态(回到未对战)
            flushed = _flush_battle_pkts()
            # 在清空状态前, 取本场最终双方当前精灵 HP, 用于判定胜负
            with _LOCK:
                fin_my = _b().get("my")
                fin_ot = _b().get("other")
            fin_my_hp = (fin_my or {}).get("hp")
            fin_ot_hp = (fin_ot or {}).get("hp")
            # 2506 FightOverInfo 的 **winnerID 即胜者账号** —— 我方账号=>我胜; 敌方(0)=>我负. 以此为权威
            try:
                fo = parse_fight_over(data)
            except Exception:
                fo = {}
            wid = fo.get("winnerID")
            if wid == me_id:
                verdict = "我方胜利"
            elif wid == 0:
                verdict = "我方战败 (或未开打结束)"
            elif fin_ot_hp is not None and fin_ot_hp <= 0 and (fin_my_hp or 0) > 0:
                verdict = "我方胜利"
            elif fin_my_hp is not None and fin_my_hp <= 0 and (fin_ot_hp or 0) > 0:
                verdict = "我方战败"
            elif fin_my_hp is not None and fin_ot_hp is not None and fin_my_hp <= 0 and fin_ot_hp <= 0:
                verdict = "同归于尽"
            else:
                verdict = "对战结束(胜负未判定)"
            _report(f"对战结束 —— 结果: {verdict}")
            with _LOCK:
                _b().update({"active": False, "finished": True, "mode": 0, "my": None, "other": None,
                                "myTeam": [], "otherTeam": [], "mySkills": [],
                                "mySkillPP": {}, "otherSkillPP": {},
                                "_ready_sent_mode": None, "lastCmd": 2506})
                _b()["version"] += 1
            log("info", "对战(2506): 对战结束, 已重置对战状态")
        elif cmd in (2405, 2394, 2410, 2507, 2508, 2404):
            # 其它回合/技能相关包: 仅更新状态, 不进精简战报
            with _LOCK:
                _b()["lastCmd"] = cmd
                _b()["version"] += 1
    except Exception as e:
        log("error", f"解析对战包({cmd})失败: {e}")


def _active_skills(battle):
    """取我方当前出战精灵的可用技能列表.

    优先取 2504 的 my((一)当前出战), 其技能在 2503 的 myTeam 里按 catchTime 匹配;
    匹配不到则取 myTeam 第一只的技能.
    """
    my = battle.get("my") or {}
    catch = my.get("catchTime")
    for p in battle.get("myTeam", []):
        if catch is not None and p.get("catchTime") == catch:
            return list(p.get("skills") or [])
    if battle.get("myTeam"):
        return list(battle["myTeam"][0].get("skills") or [])
    return []


def _apply_hp_updates(updates):
    """按 catchTime 把 2505/回合包里的 hp/maxHp 写回 _b() 的 my/other 与双方队伍."""
    if not updates:
        return
    by_ct = {u["catchTime"]: u for u in updates}

    def upd(entry):
        if not entry:
            return
        u = by_ct.get(entry.get("catchTime"))
        if u:
            entry["hp"] = u["hp"]
            entry["maxHp"] = u["maxHp"]
            # 同步大写 maxHP, 供前端血条读取 (前端读 p.maxHP)
            entry["maxHP"] = u["maxHp"]

    upd(_b().get("my"))
    upd(_b().get("other"))
    for p in _b().get("myTeam", []):
        upd(p)
    for p in _b().get("otherTeam", []):
        upd(p)


def _apply_attack_hp(blocks, me_id):
    """把 2505 AttackValue.remainHP/maxHp 按 userID 写回当前精灵 (my/other).

    AttackValue 的 remainHP 是**施法者本回合结算后**的权威血量; 仅用 catchTime 扫描
    (hpUpdates) 可能漏掉"换宠上来"的精灵, 导致其当前血量停留在满值. 这里按 userID
    直接匹配到 my(我方账号)/other(敌方0) 并把血量写回.
    """
    if not blocks:
        return
    for bk in blocks:
        uid = bk.get("userID")
        remain = bk.get("remainHP")
        mh = bk.get("maxHp")
        if remain is None or mh is None:
            continue
        if uid == me_id:
            tgt = _b().get("my")
        else:
            tgt = _b().get("other")
        if tgt is not None:
            tgt["hp"] = remain
            tgt["maxHp"] = mh
            tgt["maxHP"] = mh


def _apply_skill_pp(blocks, me_id):
    """把 2505 AttackValue.skillList 的 [技能id, 当前pp] 同步为当前精灵的剩余 PP 表.

    AttackValue.skillList 的第二个元素是服务器下发的**技能当前剩余 PP**
    (实测: 用过的技能 PP 递减, 未用则为满). 这里按 userID 存到 _b() 的
    mySkillPP / otherSkillPP (dict: {sid: 当前pp}), 前端据此同步显示 PP.
    """
    for key in ("mySkillPP", "otherSkillPP"):
        _b()[key] = {}
    if not blocks:
        return
    for bk in blocks:
        uid = bk.get("userID")
        sl = bk.get("skillList") or []
        pp_map = {}
        for sid, pp in sl:
            pp_map[str(sid)] = pp
        if uid == me_id:
            _b()["mySkillPP"] = pp_map
        else:
            _b()["otherSkillPP"] = pp_map


def _report(msg, clear=False, limit=500):
    """往 _b()(当前账号对战状态).report 追加一条战报; clear=True 时先清空."""
    import time as _t
    with _LOCK:
        if clear:
            _b()["report"] = []
        _b()["report"].append({"t": _t.strftime("%H:%M:%S"), "msg": msg})
        if len(_b()["report"]) > limit:
            _b()["report"] = _b()["report"][-limit:]


def _capture_battle(cmd, body_hex, direction="RECV"):
    """把**解密后**的完整对战包体追加写入 webui_logs/battle_capture.log, 并暂存到本场缓冲.

    存的是已解出包体的 hex(不含包头), 可直接按 [包体] 解码. 每场结束(_flush_battle_pkts)
    会把本场完整包单独立档.
    """
    import time as _t
    ts = _t.strftime('%Y-%m-%d %H:%M:%S')
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(os.path.join(_LOG_DIR, "battle_capture.log"), "a", encoding="utf-8") as f:
            f.write(f"{ts} {direction} cmd={cmd} len={len(body_hex)//2} BODY {body_hex}\n")
    except OSError:
        pass
    try:
        _BATTLE_PKTS.append((ts, direction, int(cmd), body_hex))
    except Exception:
        pass


def _flush_battle_pkts():
    """把本场对战收到的完整(解密后)包体写入 webui_logs/battle_<时间>.log, 供研究分析."""
    import time as _t
    if not _BATTLE_PKTS:
        return None
    ts = _t.strftime("%Y%m%d_%H%M%S")
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        path = os.path.join(_LOG_DIR, f"battle_{ts}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 赛尔号对战完整包体(解密后) | 保存 {ts} | 共 {len(_BATTLE_PKTS)} 包\n")
            for t, direction, cmd, body in _BATTLE_PKTS:
                name = CMD_MAP.get(cmd, "")
                f.write(f"{t} {direction} cmd={cmd} {name} len={len(body)//2} BODY {body}\n")
        n = len(_BATTLE_PKTS)
        _BATTLE_PKTS.clear()
        return (path, n)
    except OSError as e:
        print(f"保存对战包失败: {e}")
        return None



def _load_cmdmap():
    """加载 Command.cs 解析出的 id->命令名 字典 (refs/seerpacket/cmdmap.json 的副本)."""
    try:
        with open(_CMDMAP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return {int(k): v for k, v in d.items()}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


CMD_MAP = _load_cmdmap()  # {int id: str name}
CMD_NAME = {v: k for k, v in CMD_MAP.items()}  # 反向: name -> id

# 精灵名表 (id->名字): 基础来自 assets_updater 自动导出的 petbook.json (从游戏图鉴 petbook.bytes 解析),
# 再用用户可在根目录维护的 pet_names.json 覆盖同名项 (用户纠错优先).
_PETNAMES_FILE = os.path.join(_DATA_DIR, "pet_names.json")
_PETBOOK_FILE = os.path.join(_DATA_DIR, "petbook.json")

# 精灵属性表 (物种id->属性名, 如 "草"/"水"/"水 龙"): 来自 monsters.bytes 的 type 字段,
# 由 assets_updater 自动导出 (参考 refs/monsters.json 结构: type 即属性, real_id==0 的基表记录为物种本体).
_PETATTR_FILE = os.path.join(_DATA_DIR, "pet_attr.json")


def _read_json_map(path):
    import json as _json
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, ValueError, _json.JSONDecodeError):
        return {}


set_pet_names(_read_json_map(_PETBOOK_FILE))   # 自动导出的图鉴名字 (基础)
merge_pet_names(_read_json_map(_PETNAMES_FILE))  # 用户覆盖 (同名优先)

_PETATTR = _read_json_map(_PETATTR_FILE)  # {str 物种id: str 属性名}

# 技能表 (技能id->技能数据: name/pp/typeName/power/accuracy/crit/mustHit/priority/effects):
# 来自 moves.bytes + skill_effect.bytes, 由 assets_updater 自动导出 skills.json.
_SKILLS_FILE = os.path.join(_DATA_DIR, "skills.json")
_SKILLS = _read_json_map(_SKILLS_FILE)   # {str 技能id: dict 技能数据}


def skill_of(mid):
    """技能 id -> 技能数据 dict. 查不到返回 None."""
    if mid is None:
        return None
    return _SKILLS.get(str(mid)) or _SKILLS.get(mid)


# 魂印/专属特性表 (精灵物种id -> [魂印数据 {id,tags,desc,analyze,effectId,args}]):
# 来自 effecticon.bytes + effectag.bytes, 由 assets_updater 自动导出 soulmarks.json.
_SOULMARKS_FILE = os.path.join(_DATA_DIR, "soulmarks.json")
_SOULMARKS = _read_json_map(_SOULMARKS_FILE)   # {str 物种id: [魂印数据...]}


def _reload_data_maps():
    """重新读取 data/ 下的精灵数据(更新后调用).

    ``_PETATTR/_SKILLS/_SOULMARKS/_PETBOOK`` 在模块加载时读入; 而 ``ensure_pet_avatars``
    是在 main() 里(模块加载之后)才生成这些 json 的。全新克隆首次启动时, 模块加载时这些文件还不存在,
    若不重读, 界面会一直显示"无属性/无技能/无魂印"。此函数在启动更新后重新读一遍, 让首次部署即生效。
    """
    global _PETATTR, _SKILLS, _SOULMARKS
    try:
        set_pet_names(_read_json_map(_PETBOOK_FILE))
        merge_pet_names(_read_json_map(_PETNAMES_FILE))
        _PETATTR = _read_json_map(_PETATTR_FILE)
        _SKILLS = _read_json_map(_SKILLS_FILE)
        _SOULMARKS = _read_json_map(_SOULMARKS_FILE)
    except Exception as e:
        log("error", f"重读精灵数据失败: {e}")


def soulmark_of(sid):
    """精灵物种 id -> 魂印(专属特性)列表. 查不到返回 []."""
    if sid is None:
        return []
    return _SOULMARKS.get(str(sid)) or _SOULMARKS.get(sid) or []


def attr_of(sid):
    """物种 id -> 属性名 (如水/火/龙/水 龙). 查不到返回 ''."""
    if sid is None:
        return ""
    return _PETATTR.get(str(sid)) or _PETATTR.get(sid) or ""


def load_creds():
    """读取已保存的账号密码列表 [{account,password}, ...]."""
    try:
        with open(_CRED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("accounts", []) if isinstance(data, dict) else data
    except (OSError, json.JSONDecodeError):
        return []


def save_creds(account, password):
    """登录成功后保存 (或更新) 一组登录凭据, 同名账号仅保留一条. 返回当前列表."""
    accounts = load_creds()
    # 去掉同账号的旧记录
    accounts = [a for a in accounts if a.get("account") != account]
    accounts.append({"account": account, "password": password})
    try:
        with open(_CRED_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"保存账号密码失败: {e}")
    return accounts


def write_addr_file(host, port):
    """把后端实际监听地址写入 webui_addr.json, 供 PySeer 脚本运行时自动定位.

    ``--port 0`` 会自动选空闲端口, 因此实际端口到启动后才确定; 这里把最终地址
    持久化下来, 脚本无需硬编码 ``http://127.0.0.1:8680``.
    """
    try:
        with open(_ADDR_FILE, "w", encoding="utf-8") as f:
            json.dump({"url": f"http://{host}:{port}", "host": host, "port": port},
                      f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"写入后端地址文件失败: {e}")
        return False


def save_logs(reason="shutdown"):
    """把当前内存日志写入带时间戳的文件, 并清空内存日志.

    仅在服务中断/退出时调用; 刷新页面与"清空输出"按钮不影响这里保存的完整日志.
    """
    with _COND:
        entries = list(_LOG)
        _LOG.clear()
    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(_LOG_DIR, exist_ok=True)
    path = os.path.join(_LOG_DIR, f"seer_debug_{ts}.log")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 赛尔号协议调试日志 | 保存时间 {ts} | 原因 {reason}\n")
            for e in entries:
                line = f"{e.get('t', '')} [{e.get('level', 'info')}] {e.get('msg', '')}"
                if e.get("direction"):
                    line += f"  <- {e.get('direction')} cmd={e.get('cmd')}"
                if e.get("body"):
                    line += f" body={e.get('body')[:48]}"
                f.write(line + "\n")
        print(f"[{reason}] 日志已保存: {path} ({len(entries)} 条)")
    except OSError as e:
        print(f"[{reason}] 保存日志失败: {e}")


def log(level, message, **extra):
    global _SEQ
    _SEQ += 1
    entry = {
        "seq": _SEQ,
        "t": time.strftime("%H:%M:%S"),
        "level": level,
        "msg": message,
        **extra,
    }
    with _COND:
        _LOG.append(entry)
        if len(_LOG) > _LOG_MAX:
            _LOG.pop(0)
        _COND.notify_all()


def set_status(status, detail="", account="", conn=""):
    with _LOCK:
        _STATE["status"] = status
        _STATE["detail"] = detail
        if account:
            _STATE["account"] = account
        if conn:
            _STATE["conn"] = conn
    log("status", f"[{status}] {detail}")


def _is_online(client):
    """client 是否在线: 有 client 且其游戏 TCP 连接处于开启状态."""
    if client is None:
        return False
    tcp = getattr(client, "tcp", None)
    return bool(tcp and tcp.is_open())


def _relogin(account=None, host=None, port=None) -> bool:
    """用最近一次登录的 account + 记住的密码 + host/port 重新 run_login.

    主动(/api/reconnect)与被动(掉线自愈)重连共用. 成功启动重连线程返回 True.
    与 /api/login 一致: 若已在登录中则不再起新线程(防止 run_login 并发竞态).
    """
    with _LOCK:
        if _STATE["status"] == "logging_in":
            return False                 # 已有一次登录/重连在进行
        account = account or _STATE.get("account")
        host = host or _STATE.get("host") or DEFAULT_GAME_SERVER[0]
        port = int(_STATE.get("port") or DEFAULT_GAME_SERVER[1])
        _STATE["status"] = "logging_in"
        _STATE["detail"] = "正在重连..."
    if not account:
        return False
    pwd = None
    for c in load_creds():
        if c.get("account") == account:
            pwd = c.get("password")
            break
    if not pwd:
        return False
    threading.Thread(target=run_login, args=(account, pwd, host, port, None), daemon=True).start()
    return True


def _schedule_passive_reconnect():
    """被动掉线自愈: 隔 ``PASSIVE_RECONNECT_WAIT`` 秒后**自动重连**, 供脚本无感继续.

    只在仍处 disconnected 且未被人工重连时执行(若期间已被重连则跳过). 用锁防重复.
    """
    import time as _t
    global _passive_reconnect_pending, _passive_reconnect_at
    with _passive_reconnect_lock:
        if _passive_reconnect_pending:
            return
        _passive_reconnect_pending = True
    _passive_reconnect_at = _t.time()

    def _work():
        # 先观察一小段(避免"瞬断"后服务器自己恢复), 到点后仍未连上才自动重连
        try:
            _t.sleep(PASSIVE_RECONNECT_WAIT)
            with _LOCK:
                already_ready = _STATE.get("status") == "ready" and _STATE.get("connected")
            if already_ready:
                log("info", f"[被动重连] 等待期间连接已恢复, 跳过自动重连")
                return
            if _relogin():
                log("info", f"[被动重连] 掉线已 {PASSIVE_RECONNECT_WAIT}s, 自动重连中 ...")
        finally:
            global _passive_reconnect_pending
            with _passive_reconnect_lock:
                _passive_reconnect_pending = False

    threading.Thread(target=_work, daemon=True, name="seer-passive-reconnect").start()


def _mark_offline(owner, reason="连接已断开", kind="server"):
    """掉线检测: 把当前连接标记为"已断开".

    只当 ``_STATE["client"]`` 仍是被检测的这个 client 时才置为断开(避免一个旧的 listener
    在新连接建立后误把新连接标记为掉线). 断开后清空 client 让后续发包干净地报"未登录",
    但**保留 account/host/port/credentials**, 供重连复用.

    ``kind`` 记录掉线类型:
      - ``'server'`` (服务器/网络造成的**被动**掉线) -> 调 ``_schedule_passive_reconnect()``,
        隔 ``PASSIVE_RECONNECT_WAIT`` 秒后**自动重连**(后端自愈, 脚本无感);
      - ``'active'`` (我方主动中断, 如主力阵亡立刻断线) -> 不在此等待, 由调用方立即重连.
    """
    with _LOCK:
        cur = _STATE.get("client")
        if owner is not None and cur is not owner:
            return                      # 已有新连接取代本 client, 不算掉线
        _STATE["status"] = "disconnected"
        _STATE["detail"] = reason
        _STATE["connected"] = False
        _STATE["disconnect_kind"] = kind
        _STATE["client"] = None
    log("warn", f"[掉线检测] {reason}  (kind={kind})")
    if kind == "server":
        _schedule_passive_reconnect()   # 被动掉线: 后端自愈(隔 90s 自动重连)


def _start_listener(client):
    """登录后开启后台线程, 持续读取所有封包并交给 on_frame 记录 (实时监听).

    /api/send 只负责发包, 不再阻塞等待应答; 服务器的一切回包都由本线程在后台读到,
    进而实时显示到日志区与"服务器响应"表格里 (受过滤包id/收发复选框约束).
    """
    from seer.tcp_client import WebSocketClosed, WebSocketTimeout

    def _loop():
        log("info", "后台监听已开启, 实时接收所有约束之外的封包...")
        offline_reason = None
        while True:
            tcp = getattr(client, "tcp", None)
            if tcp is None or not tcp.is_open():
                offline_reason = "游戏连接已断开(tcp未开启)"
                break
            try:
                r = client.recv_game_packet(timeout=0.5)   # 触发 on_frame
                if r and r["cmd"] == 0x3EA:                 # 时间同步 -> 回应, 维持连接
                    try:
                        client.send_time_check(bytes(r["body"]).hex())
                    except Exception:
                        pass
            except WebSocketTimeout:
                continue
            except WebSocketClosed:
                offline_reason = "游戏连接被对端关闭(掉线)"
                break
            except Exception as e:
                log("error", f"监听异常: {e}")
                offline_reason = f"监听异常: {e}"
                break
        _mark_offline(client, offline_reason or "连接已断开", kind="server")   # 掉线检测(被动)

    threading.Thread(target=_loop, daemon=True, name="seer-listen").start()


# ---- "脚本"页: 默认脚本目录的列出与运行 ----
def list_scripts():
    """列出默认脚本目录(SCRIPTS_DIR)下的所有 .py 脚本文件名."""
    try:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        return sorted(f for f in os.listdir(SCRIPTS_DIR)
                      if f.endswith(".py") and os.path.isfile(os.path.join(SCRIPTS_DIR, f)))
    except OSError as e:
        log("error", f"读取脚本目录失败: {e}")
        return []


def _script_env():
    """构造脚本子进程环境: 把源码目录放 PYTHONPATH, 使脚本可 import PySeer/seer."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + (env.get("PYTHONPATH") or "")
    return env


def _run_script(name, path):
    """后台线程: 用 subprocess 运行选中脚本, 把 stdout/stderr 实时打进"脚本输出"控制台.

    每个输出都走 log("script", ...) 通道, 前端把这级日志单独渲染到"脚本输出"控制台,
    不混进封包日志. error 也单独记录到封包日志.
    """
    global _SCRIPT_PROC
    import subprocess, sys
    log("script", f"▶ 开始运行脚本 {name} ...")
    try:
        proc = subprocess.Popen(
            [sys.executable, path],
            cwd=SCRIPTS_DIR,
            env=_script_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        _SCRIPT_PROC = proc
        if proc.stdout:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    log("script", f"[{name}] {line}")
        rc = proc.wait()
        log("script", f"✔ 脚本 {name} 结束, 退出码 {rc}")
    except Exception as e:
        log("script", f"✖ 脚本 {name} 运行出错: {e}")
    finally:
        _SCRIPT_PROC = None


def _stop_script():
    """终止当前正在运行的脚本子进程 (若有)."""
    global _SCRIPT_PROC
    proc = _SCRIPT_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
            log("info", "已停止脚本")
            return True
        except Exception as e:
            log("error", f"停止脚本失败: {e}")
            return False
    log("info", "当前无脚本在运行")
    return False


# ---- 登录线程 ----
def run_login(account, password, host, port, session=None):
    set_status("logging_in", "正在连接...", account=account)
    global_client = SeerClient(account=account, password=password)
    try:
        # 把每个收发封包都打进日志 (带上命令名); 名单内的包舍弃
        def on_frame(direction, hexstr, cmd, body):
            try:
                c = int(cmd)
            except (TypeError, ValueError):
                c = -1
            if c in _FILTER_IDS:
                return                      # 舍弃: 过滤名单内的包, 不写入日志
            nm = CMD_MAP.get(c, "")
            ints = []
            try:
                ints = decode_body(bytes.fromhex(body))["ints"]
            except Exception:
                pass
            log("packet", f"{direction} cmd={cmd} {nm} body={body[:48]}",
                direction=direction, cmd=cmd, body=body, ints=ints)
            # 记录 RECV 到缓存(供脚本库 /api/send-recv 取值)
            if direction == "RECV":
                with _LOCK_RECV:
                    _RECV_SEQ[c] = _RECV_SEQ.get(c, 0) + 1
                    _RECV_LATEST[c] = body
                # 对战包: 更新 _b() 状态 (2503 队伍 / 2504 当前出战)
                if c in (2503, 2504, 2404, 2407, 2505, 2406, 2409, 2506, 2405, 2394, 2410, 2507, 2508):
                    # 解密后的完整包体落盘(不截断), 供解码 2505 等真实字节布局
                    try:
                        _capture_battle(c, body, direction)
                    except Exception:
                        pass
                    try:
                        _update_battle(c, body, str(global_client.account))
                    except Exception as eb:
                        log("error", f"更新对战状态({c})失败: {eb}")
                    # 收到对战就绪(2503) -> 自动发送 READY_TO_FIGHT(2404) 请求正式开战 (每个对战只发一次)
                    if c == 2503:
                        try:
                            mode_now = _b().get("mode")
                            if _b().get("_ready_sent_mode") != mode_now:
                                global_client.send_game_packet(2404, "")
                                _b()["_ready_sent_mode"] = mode_now
                                _report("> 自动发送 READY_TO_FIGHT(2404) 请求正式开战")
                                log("info", "[对战] 收到2503, 已自动发送 2404 READY_TO_FIGHT")
                        except Exception as e2:
                            log("error", f"自动发送2404失败: {e2}")
            # 43706(背包精灵全量)/2301(单只精灵) 应答: 解析 PetInfo 前段=能力值
            if direction == "RECV" and c in (43706, 2301):
                try:
                    data = bytes.fromhex(body)
                    if c == 43706:
                        # [第一背包数][pet1][pet2]...[第二背包数][...]
                        try:
                            bag = split_petbag_43706(data)
                            # 存入全局供"背包"分页展示 (名字用 resolve_name 回填)
                            with _LOCK:
                                for arr in (bag["first_bag"], bag["second_bag"]):
                                    for p in arr:
                                        p["name"] = resolve_name(p) or p.get("name", "")
                                _BAG["first"] = bag["first_bag"]
                                _BAG["second"] = bag["second_bag"]
                                _BAG["fetched"] = True
                                _BAG["version"] += 1
                            log("ok", f"43706 GET_PET_INFO_BY_ONCE: 第一背包 {bag['first_count']} 只, 第二背包 {bag['second_count']} 只")
                            for tag, arr in (("第一背包", bag["first_bag"]), ("第二背包", bag["second_bag"])):
                                for p in arr:
                                    log("ok", f"  [{tag}] " + format_pet(p))
                        except Exception as e2:
                            log("error", f"  解析 43706 批量失败: {e2}")
                    elif c == 2301:
                        pet, _ = parse_full(data, 0)
                        log("ok", "  2301 GET_PET_INFO 精灵: " + format_pet(pet))
                        try:
                            with _LOCK:
                                _PET_INFO[pet.get("catchTime")] = pet
                                _PET_INFO["_last"] = pet
                        except Exception:
                            pass
                except Exception as e:
                    log("error", f"解析 PetInfo 应答失败: {e}")
            # 41921 阵容列表应答: 解析并存入 _TEAMS (切换阵容弹窗用)
            if direction == "RECV" and c == 41921:
                try:
                    res = _parse_teams_41921(bytes.fromhex(body))
                    with _LOCK:
                        _TEAMS["curUsedId"] = res["curUsedId"]
                        _TEAMS["teams"] = res["teams"]
                        _TEAMS["fetched"] = True
                        _TEAMS["version"] += 1
                    log("ok", f"41921 阵容列表: {len(res['teams'])} 套, 当前使用阵容 id={res['curUsedId']}")
                    for t in res["teams"]:
                        n = sum(1 for ct, sf in t["pet_detail"] if ct)
                        mark = " [使用中]" if t["id"] == res["curUsedId"] else ""
                        log("ok", f"  阵容{t['id']} «{t['nick']}» 精灵{n}只{mark}")
                except Exception as e2:
                    log("error", f"  解析 41921 阵容失败: {e2}")
            # 2303 仓库列表应答: 分页追加到 _STORAGE
            if direction == "RECV" and c == 2303:
                try:
                    pets = _parse_storage_2303(bytes.fromhex(body))
                    with _LOCK:
                        _STORAGE["pets"].extend(pets)
                        _STORAGE["fetched"] = True
                        _STORAGE["version"] += 1
                    log("ok", f"2303 仓库列表: 本页 {len(pets)} 只, 累计 {len(_STORAGE['pets'])} 只")
                except Exception as e2:
                    log("error", f"  解析 2303 仓库列表失败: {e2}")
            # 2361 爱宠/精英仓库应答: 解析进 _EXE
            if direction == "RECV" and c == 2361:
                try:
                    pets = _parse_love_2361(bytes.fromhex(body))
                    with _LOCK:
                        _EXE["pets"] = pets
                        _EXE["fetched"] = True
                        _EXE["version"] += 1
                    log("ok", f"2361 精英(爱宠)仓库: {len(pets)} 只")
                except Exception as e2:
                    log("error", f"  解析 2361 精英仓库失败: {e2}")

        global_client.on_frame = on_frame
        if session:
            global_client.session = session
            log("ok", "使用调用方提供的 session(跳过淘米认证)")
        else:
            sess = global_client.fetch_session()
            log("ok", f"淘米 session = {sess[:16]}...")
        conn, responses = global_client.login_game(
            host, port, max_seconds=12,
            on_packet=None,   # 封包日志统一由 on_frame 记录, 避免重复
        )
        sk = getattr(global_client, "session_key", None)
        with _LOCK:
            _STATE["host"] = host
            _STATE["port"] = port
            _STATE["connected"] = True
        set_status("ready", f"已连接 {conn}; 会话密钥={sk}", account=account, conn=conn)
        # 新连接 = 新的会话: 清掉上一场(或被断掉的)对战状态, 避免 Battle(hex) 误把残留对战当成进行中
        with _LOCK:
            _BATTLE.update({"active": False, "finished": False, "mode": 0, "my": None, "other": None,
                            "myTeam": [], "otherTeam": [], "mySkills": [], "mySkillPP": {},
                            "otherSkillPP": {}, "myId": None, "lastCmd": None, "lastSkill": None,
                            "round": 0, "report": [], "_ready_sent_mode": None})
            _BATTLE["version"] += 1
        log("ok", f"登录成功! 连接={conn}  收到 {len(responses)} 个封包  会话密钥={sk}")
        # 登录后开启后台监听线程: 实时读取所有封包 (on_frame 会记录到日志与响应表格)
        _start_listener(global_client)
        # 登录后自动切换至10号阵容(防止阵容被后续开发的脚本打乱), 再刷新背包
        try:
            global_client.send_game_packet(41922, pack_body("2,10").hex())
            log("info", "已自动切换至10号阵容 (防止阵容被脚本打乱)...")
        except Exception as e:
            log("error", f"自动切换至10号阵容失败: {e}")
        # 用 43706 查询背包精灵 (出战/待命), 交给 on_frame 解析进 _BAG
        try:
            global_client.send_game_packet(43706, "")
            log("info", "已自动发送 43706 GET_PET_INFO_BY_ONCE 查询背包精灵(出战/待命)...")
        except Exception as e:
            log("error", f"自动查询背包精灵失败: {e}")
        saved = save_creds(account, password)
        log("tip", f"已记住账号 {account} (共 {len(saved)} 对); 现在可以在下面'发包测试'里手工发命令了。")
    except Exception as e:
        traceback.print_exc()
        set_status("error", f"登录失败: {e}", account=account)
        log("error", f"登录失败: {e}")
        try:
            global_client.close()
        except Exception:
            pass
    finally:
        with _LOCK:
            if _STATE["status"] == "ready":
                _STATE["client"] = global_client


# ---- HTTP 处理 ----
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默

    def _send(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path == "/api/status":
            with _LOCK:
                s = dict(_STATE)
                client = s.get("client")
            s.pop("client", None)
            s["client_present"] = client is not None
            # 在线判定: 有 client 且其游戏 TCP 连接开启
            s["connected"] = _is_online(client)
            # 若状态仍标 ready 但 socket 其实已断(异常路径未触发监听回调), 让状态回落到 disconnected
            if s["status"] == "ready" and not s["connected"]:
                s["status"] = "disconnected"
            # 被动掉线自愈信息: pending + 剩余等待秒数(供脚本/前端观察)
            import time as _now
            with _passive_reconnect_lock:
                pending = _passive_reconnect_pending
                at = _passive_reconnect_at
            s["passive_reconnect_pending"] = pending
            s["passive_reconnect_wait"] = PASSIVE_RECONNECT_WAIT
            s["passive_reconnect_in"] = int(max(0, PASSIVE_RECONNECT_WAIT - (_now.time() - at))) if pending else 0
            self._send(200, "application/json", json.dumps(s).encode("utf-8"))
        elif path == "/api/log":
            with _COND:
                body = json.dumps(_LOG, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json", body)
        elif path == "/api/credentials":
            self._send(200, "application/json", json.dumps({"accounts": load_creds()}).encode("utf-8"))
        elif path == "/api/cmdmap":
            self._send(200, "application/json", json.dumps({str(k): v for k, v in CMD_MAP.items()}).encode("utf-8"))
        elif path == "/api/filter":
            self._send(200, "application/json", json.dumps({"ok": True, "ids": sorted(_FILTER_IDS)}).encode("utf-8"))
        elif path == "/api/bag":
            with _LOCK:
                first = [_pet_bag_view(p) for p in _BAG["first"]]
                second = [_pet_bag_view(p) for p in _BAG["second"]]
                payload = {"ok": True, "first": first, "second": second,
                           "fetched": _BAG["fetched"], "version": _BAG["version"]}
            self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/teams":
            with _LOCK:
                payload = {"ok": True, "curUsedId": _TEAMS["curUsedId"],
                           "teams": _TEAMS["teams"], "fetched": _TEAMS["fetched"],
                           "version": _TEAMS["version"]}
            self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/storage":
            with _LOCK:
                pets = [_storage_view(p) for p in _STORAGE["pets"]]
                payload = {"ok": True, "pets": pets,
                           "fetched": _STORAGE["fetched"], "version": _STORAGE["version"]}
            self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/exe":
            # 2361 列表本身不带等级/天赋/性格; 用 _PET_INFO(来自 2301 养成渠道) 回填, 保证徽标/详情正常显示
            with _LOCK:
                pets = []
                for p in _EXE["pets"]:
                    v = _storage_view(p)
                    info = _PET_INFO.get(p.get("catchTime"))
                    if info:
                        for k in ("level", "dv", "nature", "is_bright"):
                            if v.get(k) is None and info.get(k) is not None:
                                v[k] = info[k]
                    pets.append(v)
                payload = {"ok": True, "pets": pets,
                           "fetched": _EXE["fetched"], "version": _EXE["version"]}
            self._send(200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/battle":
            # 对战页: 当前对战状态 (双方当前精灵/队伍/可用技能)
            with _LOCK:
                b = dict(_b())
            b["client_present"] = _STATE.get("client") is not None
            self._send(200, "application/json", json.dumps(b, ensure_ascii=False).encode("utf-8"))
        elif path.startswith("/api/pet-info"):
            # /api/pet-info?catchTime=X 返回缓存的单只 PetInfo
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            ct = int(q.get("catchTime", [0])[0])
            with _LOCK:
                info = _PET_INFO.get(ct)
            if info:
                if isinstance(info, dict) and info.get("attr") is None:
                    info["attr"] = attr_of(info.get("id"))
                self._send(200, "application/json", json.dumps({"ok": True, "pet": info}, ensure_ascii=False).encode("utf-8"))
            else:
                self._send(200, "application/json", json.dumps({"ok": False, "error": "未获取到该精灵信息"}).encode("utf-8"))
        elif path.startswith("/api/skills"):
            # /api/skills?ids=10001,10002 返回这些技能的数据 (详情/弹窗用)
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw_ids = q.get("ids", [""])[0]
            wanted = [x for x in raw_ids.split(",") if x.strip()]
            with _LOCK:
                got = {k: _SKILLS.get(k) for k in wanted if _SKILLS.get(k)}
            self._send(200, "application/json",
                       json.dumps({"ok": True, "skills": got}, ensure_ascii=False).encode("utf-8"))
        elif path.startswith("/api/soulmarks"):
            # /api/soulmarks?ids=300,3156 返回这些精灵的魂印(专属特性)数据
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw_ids = q.get("ids", [""])[0]
            wanted = [x for x in raw_ids.split(",") if x.strip()]
            with _LOCK:
                got = {k: _SOULMARKS.get(k) for k in wanted if _SOULMARKS.get(k)}
            self._send(200, "application/json",
                       json.dumps({"ok": True, "soulmarks": got}, ensure_ascii=False).encode("utf-8"))
        elif path.startswith("/head/"):
            # 精灵头像: /head/<物种id>.png
            fname = os.path.basename(path[len("/head/"):])
            if not (fname.endswith(".png") and fname[:-4].isdigit()):
                self._send(404, "text/plain", b"bad name")
                return
            fpath = os.path.join(_HEAD_DIR, fname)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                self._send(200, "image/png", data)
            else:
                self._send(404, "text/plain", b"not found")
        elif path.startswith("/effecticon/"):
            # 魂印/效果图标: /effecticon/<iconid>.png (来自 effecticon_*.bundle 提取, data/effecticon)
            fname = os.path.basename(path[len("/effecticon/"):])
            if not (fname.endswith(".png") and fname[:-4].isdigit()):
                self._send(404, "text/plain", b"bad name")
                return
            fpath = os.path.join(_EFFECT_ICON_DIR, fname)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                self._send(200, "image/png", data)
            else:
                self._send(404, "text/plain", b"not found")
        elif path == "/api/scripts":
            # "脚本"页: 默认脚本目录下的脚本列表 + 当前是否在运行
            running = _SCRIPT_PROC is not None and _SCRIPT_PROC.poll() is None
            self._send(200, "application/json",
                       json.dumps({"ok": True, "dir": SCRIPTS_DIR,
                                   "scripts": list_scripts(), "running": running},
                                  ensure_ascii=False).encode("utf-8"))
        elif path == "/api/stream":
            self.handle_sse()
        else:
            self._send(404, "text/plain", b"not found")

    def handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(b"retry: 1000\n\n")
        # 连上先回放最近 200 条历史日志 (避免面板空白, 便于核对)
        with _COND:
            recent = list(_LOG[-200:])
        for ln in recent:
            self.wfile.write(f"data: {json.dumps(ln, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()
        # 用 _LOG 的单调 seq 判"新", 不缓存 _PENDING (避免无限增长; _LOG 有 _LOG_MAX 上限)
        last_seq = recent[-1].get("seq", 0) if recent else 0
        try:
            while True:
                with _COND:
                    while True:
                        new = [e for e in _LOG if e.get("seq", 0) > last_seq]
                        if new:
                            last_seq = new[-1]["seq"]
                            break
                        _COND.wait(timeout=20)
                        if not [e for e in _LOG if e.get("seq", 0) > last_seq]:
                            # 心跳, 防止代理断连
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                for ln in new:
                    self.wfile.write(f"data: {json.dumps(ln, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, "application/json", body)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            data = self._json_body()
        except Exception as e:
            return self._send_json({"ok": False, "error": f"JSON 解析失败: {e}"}, 400)
        # 对战状态经 _b()(线程局部) 读写, 无需 global; _STATE 仍在 _LOCK 下访问.

        if path == "/api/login":
            account = str(data.get("account", "")).strip()
            password = str(data.get("password", ""))
            log("info", f"收到登录请求 account={account!r} (密码已打码)")
            if not account:
                log("error", "登录请求缺少『米米号』")
                return self._send_json({"ok": False, "error": "缺少米米号(账号)"}, 400)
            if not password:
                log("error", "登录请求缺少『密码』")
                return self._send_json({"ok": False, "error": "缺少密码"}, 400)
            host = str(data.get("host") or DEFAULT_GAME_SERVER[0])
            port = int(data.get("port") or DEFAULT_GAME_SERVER[1])
            session = data.get("session") or None
            # 防止重复/并发登录: 若已有一次登录在进行, 直接拒绝(避免 run_login 并发竞态).
            with _LOCK:
                if _STATE["status"] == "logging_in":
                    return self._send_json({"ok": False, "error": "正在登录中，请等待完成后重试"}, 400)
                _STATE["status"] = "logging_in"
                _STATE["detail"] = "正在连接..."
                _STATE["account"] = account
                _STATE["host"] = host
                _STATE["port"] = port
            threading.Thread(target=run_login, args=(account, password, host, port, session), daemon=True).start()
            return self._send_json({"ok": True})

        elif path == "/api/disconnect":
            with _LOCK:
                cli = _ctx_client()
            if cli:
                try:
                    cli.close()
                except Exception:
                    pass
            # 清空并标记掉线(保留 account/host/port 便于 /api/reconnect 复用); 主动中断 -> kind=active
            _mark_offline(cli, "已主动断开(可立即重连)", kind="active")
            log("info", "已断开当前连接")
            return self._send_json({"ok": True})

        elif path == "/api/reconnect":
            # 断线重连(主动): 用最近一次登录的 account + 记住的密码 + host/port 重新 run_login.
            # 供"战斗中主力阵亡 -> 断线 -> 重连"策略使用(掉线后主力不判死, 可重打同一关).
            # 注意: **被动**掉线由后端 _schedule_passive_reconnect 自愈, 无需脚本触发本接口.
            with _LOCK:
                cur_status = _STATE.get("status")
            if cur_status == "logging_in":
                return self._send_json({"ok": False, "error": "正在登录中, 请稍后再重连"}, 400)
            if not _relogin():
                return self._send_json({"ok": False,
                                        "error": "当前无账号或缺少可用的登录凭据, 无法主动重连"}, 400)
            log("info", "[重连] 主动重连: 正在重新登录 ...")
            return self._send_json({"ok": True, "msg": "正在重连"})


        elif path == "/api/credentials/delete":
            with _COND:
                acc = str(data.get("account", ""))
            accounts = [a for a in load_creds() if a.get("account") != acc]
            try:
                with open(_CRED_FILE, "w", encoding="utf-8") as f:
                    json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)
                log("info", f"已删除记住的账号 {acc}")
            except OSError as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            return self._send_json({"ok": True, "accounts": accounts})

        elif path == "/api/send":
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录, 无法发包"}, 400)
            try:
                raw_cmd = str(data.get("cmd")).strip()
                # 支持按名字输入: "ENTER_MAP" / "40001" / "ENTER_MAP (2001)"
                name_id = raw_cmd.split("(")[0].strip()
                if name_id.isdigit():
                    cmd = int(name_id)
                else:
                    if name_id in CMD_NAME:
                        cmd = CMD_NAME[name_id]
                    else:
                        return self._send_json({"ok": False,
                                                "error": f"未知命令名: {name_id!r}"}, 400)
                body_hex = data.get("body", "")
                # body 当前是"参数列表"输入: 按标准格式打包成十六进制; 也可加 prefix=h: 直接给原始HEX
                encode = str(data.get("encode", "pack"))
                if encode == "hex":
                    body_hex = body_hex       # 原样作为十六进制
                else:
                    ok, res = pack_body(body_hex, raise_on_error=False)
                    if not ok:
                        return self._send_json({"ok": False, "error": f"包体参数错误: {res}"}, 400)
                    body_hex = res.hex() if isinstance(res, bytes) else res
                cli.send_game_packet(cmd, body_hex)   # 收发封包由 on_frame 统一记录, 避免重复
                # 不再阻塞等待应答: 服务器的一切回包由后台监听线程实时读到,
                # 经 on_frame 记录后显示到日志区与"服务器响应"表格 (受过滤/收发复选框约束)。
                return self._send_json({"ok": True, "sent": {"cmd": cmd, "body": body_hex}})
            except Exception as e:
                log("error", f"发包异常: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/send-recv":
            # 脚本库用: 发送 SEND 包并等待该命令的 RECV 应答, 返回完整包体(hex) + 十进制 ints.
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录, 无法发包"}, 400)
            try:
                raw_cmd = str(data.get("cmd")).strip()
                name_id = raw_cmd.split("(")[0].strip()
                if name_id.isdigit():
                    cmd = int(name_id)
                else:
                    cmd = CMD_NAME.get(name_id)
                    if cmd is None:
                        return self._send_json({"ok": False, "error": f"未知命令名 {name_id!r}"}, 400)
                body_spec = data.get("body", "")
                ok2, packed = pack_body(body_spec, raise_on_error=False)
                if not ok2:
                    return self._send_json({"ok": False, "error": f"包体参数错误: {packed}"}, 400)
                body_hex = packed.hex() if isinstance(packed, bytes) else packed
                timeout = float(data.get("timeout", 8))
                with _LOCK_RECV:
                    before_seq = _RECV_SEQ.get(cmd, 0)
                cli.send_game_packet(cmd, body_hex)
                log("info", f"[send-recv] 已发送 {cmd} {CMD_MAP.get(cmd,'')}, 等待 RECV...")
                resp = None
                deadline = time.time() + timeout
                while time.time() < deadline:
                    with _LOCK_RECV:
                        if _RECV_SEQ.get(cmd, 0) > before_seq:
                            resp = _RECV_LATEST.get(cmd)
                            break
                    time.sleep(0.05)
                if resp is None:
                    return self._send_json({"ok": False, "error": "等待响应超时"}, 504)
                ints = decode_body(bytes.fromhex(resp))["ints"]
                return self._send_json({"ok": True, "body": resp, "ints": ints,
                                        "cmd": cmd, "name": CMD_MAP.get(cmd, "")})
            except Exception as e:
                log("error", f"send-recv 异常: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/teams/fetch":
            # 拉取阵容列表: 发 41921 [0], 由监听线程解析进 _TEAMS
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                cli.send_game_packet(41921, pack_body("0").hex())
                log("info", "已发送 41921 拉取阵容列表...")
                return self._send_json({"ok": True})
            except Exception as e:
                log("error", f"拉取阵容失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/teams/switch":
            # 切换阵容: 发 41922 [2, 阵容id]
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                tid = int(data.get("id"))
                cli.send_game_packet(41922, pack_body(f"2,{tid}").hex())
                log("info", f"已发送 41922 切换阵容 [2, {tid}]...")
                # 切换后同步刷新背包精灵 (43706), 让出战/待命背包反映新阵容
                try:
                    cli.send_game_packet(43706, "")
                    log("info", "已发送 43706 同步刷新背包精灵...")
                except Exception as e2:
                    log("error", f"切换后刷新背包精灵失败: {e2}")
                return self._send_json({"ok": True, "sent": {"cmd": 41922, "team": tid}})
            except Exception as e:
                log("error", f"切换阵容失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pets/store":
            # 入库: 发 PET_RELEASE(2304) [catchTime, posIndex] (0=第一背包->仓库, 3=第二背包->仓库)
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                catch = int(data.get("catchTime"))
                bag = str(data.get("bag", "first"))
                pos = 0 if bag != "second" else 3
                cli.send_game_packet(2304, pack_body(f"{catch},{pos}").hex())
                log("info", f"已发送入库(2304 PET_RELEASE) [{catch},{pos}]...")
                # 完成后刷新背包
                try:
                    cli.send_game_packet(43706, "")
                    log("info", "入库后已发送 43706 刷新背包...")
                except Exception as e2:
                    log("error", f"入库后刷新背包失败: {e2}")
                return self._send_json({"ok": True, "sent": {"cmd": 2304, "catchTime": catch, "pos": pos}})
            except Exception as e:
                log("error", f"入库失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pets/default":
            # 设为首发: 发 PET_DEFAULT(2308) [catchTime]
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                catch = int(data.get("catchTime"))
                cli.send_game_packet(2308, pack_body(str(catch)).hex())
                log("info", f"已发送设为首发(2308 PET_DEFAULT) [{catch}]...")
                try:
                    cli.send_game_packet(43706, "")
                    log("info", "设为首发后已发送 43706 刷新背包...")
                except Exception as e2:
                    log("error", f"设为首发后刷新背包失败: {e2}")
                return self._send_json({"ok": True, "sent": {"cmd": 2308, "catchTime": catch}})
            except Exception as e:
                log("error", f"设为首发失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/storage/fetch":
            # 拉取仓库列表: 发 2303 GET_PET_LIST 分页 (0-6000, 每1000一页), 由监听线程解析进 _STORAGE
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                with _LOCK:
                    _STORAGE["pets"] = []
                for start in range(0, 6000, 1000):
                    cli.send_game_packet(2303, pack_body(f"{start},{start+1000}").hex())
                log("info", "已发送 2303 GET_PET_LIST 拉取仓库(0-6000)...")
                return self._send_json({"ok": True})
            except Exception as e:
                log("error", f"拉取仓库失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/exe/fetch":
            # 拉取精英(爱宠)仓库: 发 2361 GET_LOVE_PET_LIST, 由监听线程解析进 _EXE
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                cli.send_game_packet(2361, "")
                log("info", "已发送 2361 GET_LOVE_PET_LIST 拉取精英仓库...")
                return self._send_json({"ok": True})
            except Exception as e:
                log("error", f"拉取精英仓库失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pet-info/fetch":
            # 拉取单只精灵完整信息: 发 2301 GET_PET_INFO [catchTime], 监听线程缓存进 _PET_INFO.
            # 已缓存则跳过, 避免重复拉取 (养成信息缓存).
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                catch = int(data.get("catchTime"))
                with _LOCK:
                    if catch in _PET_INFO:
                        log("info", f"2301 GET_PET_INFO [{catch}] 已有缓存, 跳过")
                        return self._send_json({"ok": True, "cached": True})
                cli.send_game_packet(2301, pack_body(str(catch)).hex())
                log("info", f"已发送 2301 GET_PET_INFO [{catch}]...")
                return self._send_json({"ok": True})
            except Exception as e:
                log("error", f"拉取精灵信息失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pets/warehouse-swap":
            # 仓库精灵 <-> 背包精灵 互换: 先退背包精灵入库, 再把仓库精灵取出到该背包
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                bag_catch = int(data.get("bagCatchTime"))
                bag = str(data.get("bag", "first"))
                st_catch = int(data.get("storageCatchTime"))
                bg_pos = 3 if bag == "second" else 0   # 背包精灵退仓库: 0=第一->仓库, 3=第二->仓库
                rg_pos = 2 if bag == "second" else 1   # 仓库精灵进背包: 1=仓库->第一, 2=仓库->第二
                cli.send_game_packet(2304, pack_body(f"{bag_catch},{bg_pos}").hex())
                cli.send_game_packet(2304, pack_body(f"{st_catch},{rg_pos}").hex())
                log("info", f"已发送仓库互换: 退背包[{bag_catch},{bg_pos}] 取仓库[{st_catch},{rg_pos}]...")
                try:
                    cli.send_game_packet(43706, "")   # 刷新背包
                except Exception:
                    pass
                # 仓库列表由前端 fetchStorage 按 2303 分页重拉
                return self._send_json({"ok": True, "sent": {"cmd": 2304, "pair": [bag_catch, bg_pos, st_catch, rg_pos]}})
            except Exception as e:
                log("error", f"仓库互换失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pets/swap":
            # 切换两只精灵位置: 发 41462 [sortIndex1, catchTime1, sortIndex2, catchTime2]
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                sort1 = int(data.get("sortIndex1"))
                catch1 = int(data.get("catchTime1"))
                sort2 = int(data.get("sortIndex2"))
                catch2 = int(data.get("catchTime2"))
                cli.send_game_packet(41462, pack_body(f"{sort1},{catch1},{sort2},{catch2}").hex())
                log("info", f"已发送切换位置(41462) [{sort1},{catch1},{sort2},{catch2}]...")
                # 完成后刷新背包
                try:
                    cli.send_game_packet(43706, "")
                    log("info", "切换后已发送 43706 刷新背包...")
                except Exception as e2:
                    log("error", f"切换后刷新背包失败: {e2}")
                return self._send_json({"ok": True, "sent": {"cmd": 41462, "pair": [sort1, catch1, sort2, catch2]}})
            except Exception as e:
                log("error", f"切换位置失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/pets/move":
            # 拖到另一背包空位 => 直接移动: 仓库->背包 用2304(取仓库到背包); 背包->另一背包 用41462(目标空位catchTime=0)
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                kind = data.get("kind")
                catch = int(data.get("catchTime"))
                if kind == "storage":
                    bag = str(data.get("bag", "first"))
                    rg_pos = 2 if bag == "second" else 1   # 仓库->第二/第一背包 (对齐 warehouse-swap 的 rg_pos)
                    cli.send_game_packet(2304, pack_body(f"{catch},{rg_pos}").hex())
                    log("info", f"仓库精灵 id={catch} 移至{bag}背包 (2304 [{catch},{rg_pos}])...")
                else:
                    from_sort = int(data.get("fromSort"))
                    to_sort = int(data.get("toSort"))
                    # 背包 -> 另一背包空位: 用"先入库、再取出到目标包"的 2304 两步(与 warehouse-swap /
                    # storage->bag 相同的**已确认**机制), 而不是未实测的 41462 目标 catchTime=0(在实测中不生效)。
                    # 来源/目标包由 from/to_sort 推导: sort 1..6=第一背包, 7..12=第二背包.
                    src_bag = "second" if from_sort > 6 else "first"
                    dst_bag = "second" if to_sort > 6 else "first"
                    put_pos = 3 if src_bag == "second" else 0   # 背包精灵退仓库: 0=第一, 3=第二
                    get_pos = 2 if dst_bag == "second" else 1   # 仓库精灵进背包: 1=第一, 2=第二
                    cli.send_game_packet(2304, pack_body(f"{catch},{put_pos}").hex())
                    cli.send_game_packet(2304, pack_body(f"{catch},{get_pos}").hex())
                    log("info", f"背包精灵 id={catch} 由 {src_bag} -> 仓库 -> {dst_bag} "
                                f"(2304 [{catch},{put_pos}] 再 [{catch},{get_pos}])...")
                try:
                    cli.send_game_packet(43706, "")
                except Exception:
                    pass
                return self._send_json({"ok": True, "sent": {"kind": kind,
                    "cmd": 2304 if kind == "storage" else 41462, "catchTime": catch}})
            except Exception as e:
                log("error", f"移动精灵失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/body-preview":
            # 前端实时预览: 把参数列表打包成标准包体并显示其十六进制
            try:
                spec = str(data.get("spec", ""))
                ok, packed = pack_body(spec, raise_on_error=False)
                if not ok:
                    return self._send_json({"ok": False, "error": packed}, 400)
                body = packed
                try:
                    parts = parse_parts(spec)
                except ValueError as e:
                    parts = [str(e)]
                return self._send_json({"ok": True, "hex": body.hex(), "length": len(body),
                                        "parts": parts}, 200)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/filter":
            # 保存过滤包id名单 (逗号/空白分隔或数组); 名单内的包将被舍弃
            global _FILTER_IDS
            raw = data.get("ids", "")
            if isinstance(raw, list):
                ids = [int(x) for x in raw if str(x).strip().isdigit() or str(x).strip().lstrip("-").isdigit()]
            else:
                ids = [int(t.strip()) for t in str(raw).replace(",", " ").split() if t.strip()]
            _FILTER_IDS = set(ids)
            _save_filter(_FILTER_IDS)
            log("info", f"已更新并保存过滤包id名单: {sorted(_FILTER_IDS)}")
            return self._send_json({"ok": True, "ids": sorted(_FILTER_IDS)})

        elif path == "/api/scripts/run":
            # 运行默认脚本目录下的一个 .py 脚本 (后台子进程, 输出实时进日志)
            name = str(data.get("name", "")).strip()
            if not name:
                return self._send_json({"ok": False, "error": "缺少脚本名"}, 400)
            base = os.path.realpath(SCRIPTS_DIR)
            target = os.path.realpath(os.path.join(SCRIPTS_DIR, name))
            if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
                return self._send_json({"ok": False, "error": "非法或不存在脚本"}, 400)
            if _SCRIPT_PROC is not None and _SCRIPT_PROC.poll() is None:
                return self._send_json({"ok": False, "error": "已有脚本在运行"}, 400)
            threading.Thread(target=_run_script, args=(name, target), daemon=True).start()
            return self._send_json({"ok": True, "name": name})

        elif path == "/api/scripts/stop":
            # 停止当前正在运行的脚本子进程
            return self._send_json({"ok": True, "stopped": _stop_script()})

        elif path == "/api/battle/send":
            # 对战页发包: {cmd, body, encode} (命令号可为任意对战命令, 用当前连接发送)
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录, 无法发包"}, 400)
            try:
                raw_cmd = str(data.get("cmd")).strip()
                cmd = int(raw_cmd) if raw_cmd.isdigit() else CMD_NAME.get(raw_cmd)
                if cmd is None:
                    return self._send_json({"ok": False, "error": f"未知命令名/号: {raw_cmd!r}"}, 400)
                body_spec = str(data.get("body", ""))
                encode = str(data.get("encode", "pack"))
                if encode == "hex":
                    body_hex = "".join(ch for ch in body_spec if ch in "0123456789abcdefABCDEF")
                else:
                    ok, packed = pack_body(body_spec, raise_on_error=False)
                    if not ok:
                        return self._send_json({"ok": False, "error": f"包体参数错误: {packed}"}, 400)
                    body_hex = packed.hex() if isinstance(packed, bytes) else packed
                cli.send_game_packet(cmd, body_hex)
                log("info", f"[对战] 已发送 cmd={cmd} {CMD_MAP.get(cmd,'')} body={body_hex[:48]}")
                return self._send_json({"ok": True, "sent": {"cmd": cmd, "body": body_hex}})
            except Exception as e:
                log("error", f"[对战] 发包异常: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/battle/hex":
            # 发起对战: 输入"带cmdid的完整HEX包", 从包头提取命令号+包体, 重建(decode)后经当前连接发送.
            # 支持任意对战命令号 (不止 41129); uid/序列号会用当前账号重算.
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录, 无法发包"}, 400)
            # 发起对战即视为"新一场对战开始": 清掉上一场遗留的结束标记与最后回合数据.
            # 否则脚本端(PySeer.Battle)在等待本场 2503 期间, 会把它误判成"上一场结束包"
            # (二次运行会因此抛"对战未能正常进入(收到了结束包)")。
            with _LOCK:
                _b()["finished"] = False
                _b()["lastSkill"] = None
            try:
                hexs = "".join(ch for ch in str(data.get("hex", "")) if ch in "0123456789abcdefABCDEF")
                raw = bytes.fromhex(hexs)
                if len(raw) < 17:
                    return self._send_json({"ok": False, "error": "HEX 过短(需含 17 字节包头)"}, 400)
                cmd = int.from_bytes(raw[5:9], "big")       # [len4][ver1][cmd4]... 从串口提取命令号
                body = raw[17:]
                cli.send_game_packet(cmd, body.hex())        # 会把 uid/序列号按当前账号重建并加密封包
                log("info", f"[对战] 发起: 解析出 cmd={cmd} {CMD_MAP.get(cmd,'')} 包体={body.hex()[:48]}")
                return self._send_json({"ok": True, "sent": {"cmd": cmd, "body": body.hex()}})
            except Exception as e:
                log("error", f"[对战] 发起HEX失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/battle/clear":
            # 清空对战状态
            with _LOCK:
                _b().update({"active": False, "finished": False, "mode": 0, "my": None, "other": None,
                                "myTeam": [], "otherTeam": [], "mySkills": [],
                                "mySkillPP": {}, "otherSkillPP": {}, "myId": None, "lastCmd": None,
                                "lastSkill": None})
                _b()["version"] += 1
            return self._send_json({"ok": True})

        elif path == "/api/battle/action":
            # 记录一条客户端动作到战报 (如点击技能), 便于回放/观察
            msg = str(data.get("msg", "")).strip()
            if msg:
                _report(msg)
                return self._send_json({"ok": True, "msg": msg})
            return self._send_json({"ok": False, "error": "msg 为空"}, 400)

        elif path == "/api/battle/change-pet":
            # 换宠: 发 2407 CHANGE_PET, 包体 = 目标精灵的 catchTime (int32 大端).
            # 依反编译 PlayerModel.changePet / setAutoChangePet: 客户端就发这一个 int.
            # 参数: {id: 物种id} 由后端从当前对战阵容(myTeam)里查一只可用该id精灵并取它的 catchTime;
            #       也可直接 {catchTime: 目标精灵catchTime}. 两者取其一.
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录, 无法发包"}, 400)
            try:
                sid = data.get("id")
                catch_raw = str(data.get("catchTime", "")).strip()
                if sid is not None:
                    # 从"当前对战阵容"myTeam 里找一只该物种id的可用精灵(排除已在场上的当前精灵)
                    sid = int(sid)
                    my_team = _b().get("myTeam") or []
                    cur_ct = (_b().get("my") or {}).get("catchTime")
                    cands = [p for p in my_team
                             if p.get("id") == sid and p.get("catchTime")
                             and p.get("catchTime") != cur_ct]
                    if not cands:
                        return self._send_json(
                            {"ok": False,
                             "error": f"对战阵容中找不到可用(非当前出战)的精灵 id={sid}"}, 400)
                    alive = [p for p in cands if (p.get("hp") or 0) > 0]
                    cand = alive[0] if alive else cands[0]      # 优先存活, 否则任取一只
                    catch = int(cand["catchTime"])
                elif catch_raw:
                    catch = int(catch_raw)                       # 直接给 catchTime
                else:
                    return self._send_json({"ok": False, "error": "缺少精灵 id 或 catchTime"}, 400)
                body_hex = pack_body(str(catch)).hex()      # int32 大端, 4B
                cli.send_game_packet(2407, body_hex)
                log("info", f"[对战] 换宠请求: 2407 CHANGE_PET id={sid} catchTime={catch} body={body_hex}")
                return self._send_json({"ok": True,
                                        "sent": {"cmd": 2407, "id": sid, "catchTime": catch,
                                                 "body": body_hex}})
            except Exception as e:
                log("error", f"[对战] 换宠请求失败: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        elif path == "/api/battle/wait":
            # 脚本库对战体用: 阻塞等待"对战状态变化" (version 递增 或 对战结束).
            # 返回最新 _b() 快照; changed=False 表示超时(该时段无新对战事件).
            with _LOCK:
                cli = _ctx_client()
            if cli is None:
                return self._send_json({"ok": False, "error": "尚未登录"}, 400)
            try:
                from_version = int(data.get("version", 0))
                timeout = float(data.get("timeout", 8))
                deadline = time.time() + timeout
                ver, fin, b, changed = 0, False, {}, False
                while time.time() < deadline:
                    with _LOCK:
                        ver = _b().get("version", 0)
                        fin = _b().get("finished", False)
                        b = dict(_b())
                    if ver > from_version or fin:
                        changed = True
                        break
                    time.sleep(0.05)
                return self._send_json({"ok": True, "changed": changed,
                                        "finished": fin, "version": ver,
                                        "battle": b})
            except Exception as e:
                log("error", f"[对战] wait 异常: {e}")
                return self._send_json({"ok": False, "error": str(e)}, 400)

        return self._send_json({"ok": False, "error": "unknown"}, 404)


# ---- 前端页面 (内嵌) ----
PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>赛尔号协议调试台</title>
<script>try{var _t=localStorage.getItem('pyseer_theme')||((window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches)?'light':'dark');document.documentElement.setAttribute('data-theme',_t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}</script>
<style>
/* ============ 赛尔号协议调试台 · UI 主题 (仅样式, 不影响功能) ============ */
:root{
  --bg:#0a0f1c; --panel:#101726; --card:#121a2c; --card-2:#141d31;
  --elev:#1b2440; --elev-2:#24304f; --inset:#0a0f1c; --inset-2:#0d1322;
  --line:#2a344d; --line-2:#3b4c68;
  --text:#e6edf3; --muted:#94a1b5; --muted-2:#6b7a93;
  --accent:#5b9dff; --accent-deep:#3f7fdb; --accent-soft:#122f56;
  --green:#3ddc8f; --green-deep:#22a556;
  --amber:#f2c14e; --red:#f7696b; --purple:#d3b4ff; --cyan:#7cc4ff; --btn-fg:#06131f;
  --mono:"SFMono-Regular","JetBrains Mono","Menlo","Consolas","DejaVu Sans Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",Roboto,"Helvetica Neue",Arial,sans-serif;
  --radius:10px; --radius-sm:7px;
  --shadow:0 6px 20px rgba(0,0,0,.35); --shadow-sm:0 2px 8px rgba(0,0,0,.28);
}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);
  background-image:radial-gradient(1100px 520px at 15% -10%,#16233c 0,rgba(22,35,60,0) 55%),
                   radial-gradient(900px 480px at 100% 0,rgba(91,157,255,.08) 0,rgba(91,157,255,0) 60%);
  background-attachment:fixed;min-height:100vh;font-size:14px;line-height:1.5}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:var(--inset)}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px;border:2px solid var(--inset)}
::-webkit-scrollbar-thumb:hover{background:var(--line-2)}
::selection{background:rgba(91,157,255,.32);color:#fff}

/* ---- 顶部 ---- */
header{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:12px;
  padding:12px 20px;background:linear-gradient(180deg,#141b2d,#101726);border-bottom:1px solid var(--line);
  box-shadow:0 1px 0 rgba(255,255,255,.02),0 4px 14px rgba(0,0,0,.3)}
header .brand{display:flex;align-items:center;gap:11px}
header .logo{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent),var(--green));color:var(--btn-fg);font-weight:800;font-size:15px;
  box-shadow:0 0 14px rgba(91,157,255,.4);flex:0 0 auto}
header h1{font-size:16px;margin:0;color:var(--text);font-weight:700;letter-spacing:.3px;line-height:1.2}
header .sub{font-size:11px;color:var(--muted-2);margin-top:2px;letter-spacing:.2px}
.status{margin-left:10px;display:inline-flex;align-items:center;gap:7px;padding:5px 12px;
  border-radius:999px;font-size:12px;font-weight:600;background:var(--elev);border:1px solid var(--line);color:var(--muted)}
.status::before{content:'';width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor}
.st-idle{color:#9ab0c8}.st-logging_in{color:var(--amber)}.st-ready{color:var(--green)}.st-error{color:var(--red)}

/* ---- 标签页 ---- */
.tabs{display:flex;gap:6px;padding:14px 20px 0;background:var(--panel);border-bottom:1px solid var(--line)}
.tabs .tab{position:relative;margin:0;padding:9px 20px 10px;border:0;background:transparent;color:var(--muted);
  font:600 13px/1 var(--sans);cursor:pointer;border-radius:9px 9px 0 0;transition:color .15s,background .15s}
.tabs .tab:hover{color:var(--text);background:rgba(91,157,255,.06)}
.tabs .tab.active{color:var(--accent)}
.tabs .tab.active::after{content:'';position:absolute;left:12px;right:12px;bottom:0;height:2px;
  background:linear-gradient(90deg,var(--accent),var(--green));border-radius:2px;box-shadow:0 0 8px var(--accent)}
.tabs .tab.live::before{content:'';display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--green);margin-right:7px;box-shadow:0 0 6px var(--green);animation:blinkdot 1.1s infinite;vertical-align:middle}
@keyframes blinkdot{50%{opacity:.25}}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ---- 卡片 ---- */
main{display:flex;gap:14px;padding:18px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px;min-width:280px;box-shadow:var(--shadow-sm)}
.card h2{font-size:13px;margin:0 0 12px;color:var(--text);font-weight:600;letter-spacing:.2px;
  padding-left:9px;border-left:3px solid var(--accent);line-height:1.2}
.card h2 sup{color:var(--muted-2)}
label{font-size:12px;color:var(--muted);display:block;margin:10px 0 4px;font-weight:500}
label sup{color:var(--red)}

/* ---- 表单控件 ---- */
input[type=text],input[type=password],input[type=number],select,textarea{
  width:100%;padding:8px 10px;background:var(--inset);border:1px solid var(--line);color:var(--text);
  border-radius:var(--radius-sm);font:13px/1.4 var(--mono);transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(91,157,255,.18)}
input::placeholder,textarea::placeholder{color:var(--muted-2)}
input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px}
input[type=checkbox]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* ---- 按钮 ---- */
button{margin:8px 6px 0 0;padding:8px 14px;background:linear-gradient(180deg,var(--green),var(--green-deep));
  border:1px solid rgba(0,0,0,.15);color:var(--btn-fg);border-radius:var(--radius-sm);cursor:pointer;
  font:600 13px/1 var(--sans);transition:transform .06s,filter .15s,box-shadow .15s,background .15s;
  box-shadow:0 2px 6px rgba(25,150,90,.25)}
button:hover{filter:brightness(1.06)}
button:active{transform:translateY(1px)}
button:disabled{opacity:.45;cursor:not-allowed;filter:none;transform:none;box-shadow:none}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button.off{background:var(--elev);color:var(--text);border:1px solid var(--line);box-shadow:none}
button.off:hover{background:var(--elev-2);border-color:var(--line-2)}

/* ---- 日志 / 终端 ---- */
#log{width:100%;min-height:180px;max-height:56vh;overflow:auto;background:var(--inset);padding:9px 10px;
  border:1px solid var(--line);border-radius:var(--radius-sm);font:12.5px/1.6 var(--mono);white-space:pre-wrap}
.lvl-info{color:var(--muted)}.lvl-ok{color:var(--green)}.lvl-packet{color:var(--cyan)}
.lvl-error{color:var(--red)}.lvl-tip{color:var(--amber)}.lvl-status{color:var(--amber)}
.lvl-battle{color:var(--purple)}
#resp{width:100%;min-height:120px;background:var(--inset);padding:9px 10px;border:1px solid var(--line);
  border-radius:var(--radius-sm);font:12.5px var(--mono);color:var(--green)}

.rowflex{display:flex;gap:10px}.rowflex>*{flex:1}
.filterbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 10px}
.filterbar label{display:inline;margin:0;white-space:nowrap}
.filterbar input[type=text]{width:auto;flex:1;min-width:200px}
.filterbar input[type=checkbox]{vertical-align:middle;margin-right:4px}
.filterbar button{margin:0}

/* ---- 脚本列表 ---- */
.script-item{display:block;width:100%;text-align:left;padding:8px 12px;border:0;border-bottom:1px solid var(--line);
  background:transparent;color:var(--text);font:12.5px var(--mono);cursor:pointer;transition:background .12s}
.script-item:hover{background:var(--elev)}
.script-item.sel{background:linear-gradient(90deg,rgba(91,157,255,.16),rgba(91,157,255,.04));color:var(--accent)}

/* ---- 对战页 ---- */
.fight-side{display:flex;gap:12px;align-items:center}
.fight-card{flex:1;background:var(--inset);border:1px solid var(--line);border-radius:var(--radius);
  padding:10px;text-align:center;box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
.fight-card .fc-img{width:92px;height:92px;margin:0 auto;background:var(--card-2);border:1px solid var(--line);
  border-radius:12px;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:22px;color:var(--muted)}
.fight-card .fc-img img{width:100%;height:100%;object-fit:contain}
.fc-name{font-size:14px;margin-top:8px;color:var(--text);font-weight:700}
.fc-lv{font-size:12px;color:var(--muted);margin:2px 0}
.hpbar{height:12px;background:var(--elev);border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:6px 0}
.hpbar .hp{height:100%;background:linear-gradient(90deg,var(--green),var(--green-deep));transition:width .3s;border-radius:999px}
.hpbar .hp.low{background:linear-gradient(90deg,var(--red),#c9445a)}
.hptxt{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.fc-page{font-size:11px;color:var(--muted-2);margin-top:4px}
.battle-team{display:flex;flex-wrap:wrap;gap:8px}
.pt-chip{width:76px;background:var(--inset);border:1px solid var(--line);border-radius:9px;padding:4px;
  text-align:center;font:11px var(--mono);transition:border-color .15s}
.pt-chip:hover{border-color:var(--line-2)}
.pt-chip img{width:56px;height:56px;object-fit:contain;margin:0 auto;display:block}
.pt-chip .pn{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}
.pt-chip .ph{color:var(--muted);font-size:10px}
.skill-btn{flex:1 1 0;min-width:64px;margin:0;background:linear-gradient(180deg,var(--green),var(--green-deep));
  border:1px solid rgba(0,0,0,.15);color:var(--btn-fg);border-radius:9px;cursor:pointer;padding:8px 6px;text-align:center;
  display:flex;flex-direction:column;gap:2px;align-items:center;white-space:nowrap;overflow:hidden;
  font-family:var(--sans);box-shadow:0 2px 6px rgba(25,150,90,.2)}
.skill-btn:hover{filter:brightness(1.07)}
.skill-btn .sb-name{font-size:12px;color:var(--btn-fg);font-weight:700;text-overflow:ellipsis;overflow:hidden;max-width:100%}
.skill-btn .sb-sub{font-size:10px;color:var(--btn-fg);opacity:.82;line-height:1.1;font-weight:500}
.skill-btn .sb-pp{font-size:10px;color:var(--btn-fg);opacity:.72}
.skill-btn[disabled]{opacity:.45;cursor:not-allowed;filter:none}
.ops-btn{min-width:96px;padding:9px 14px;margin:0;background:var(--elev);border:1px solid var(--line);
  color:var(--text);border-radius:9px;cursor:pointer;font:600 13px var(--sans);transition:background .15s,border-color .15s}
.ops-btn:hover{background:var(--elev-2);border-color:var(--line-2)}
.ops-btn:disabled{opacity:.45;cursor:not-allowed}

/* ---- 背包 ---- */
.bagwrap{display:flex;gap:14px;padding:18px;flex-wrap:wrap}
.avrow{display:grid;grid-template-columns:repeat(auto-fill,88px);gap:10px;margin:0 0 14px;justify-content:start}
/* 背包（第一/第二背包）固定 3 列且均分填满面板宽度（不随页面变化）；与仓库保持相同 88px 图标尺寸 */
#bag-first,#bag-second{grid-template-columns:repeat(3,1fr)}
.av-btn{display:block;min-width:0;margin:0;padding:6px;background:var(--elev);border:1px solid var(--line);
  border-radius:9px;text-align:center;cursor:pointer;color:var(--text);overflow:hidden;font:12px var(--sans);
  line-height:1.2;touch-action:none;-webkit-tap-highlight-color:transparent;transition:border-color .15s,transform .06s}
.av-btn:hover{border-color:var(--line-2)}
.av-btn:active{transform:translateY(1px)}
.av-btn.sel{border-color:var(--accent);background:var(--accent-soft);box-shadow:0 0 0 2px rgba(91,157,255,.25)}
.av-img{width:100%;aspect-ratio:1;background:var(--inset);border:1px solid var(--line);border-radius:7px;
  display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--muted)}
.av-empty{width:100%;aspect-ratio:1;background:transparent;border:1px dashed var(--line-2);border-radius:7px;
  display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--muted-2)}
.av-txt{font-size:11px;margin-top:5px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--sans)}

.detail-grid{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:12.5px}
.detail-grid .k{color:var(--muted)}
.detail-grid .v{color:var(--text)}
.detail-sec{margin-top:10px}
.detail-sec h3{font-size:12px;color:var(--accent);margin:10px 0 6px;font-weight:600;letter-spacing:.2px}
.abgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:7px 20px}
.abcell{display:flex;align-items:center;gap:6px;font-size:12.5px}
.abcell .k{color:var(--muted)}
.abcell .v{color:var(--text);font-variant-numeric:tabular-nums}
.abcell .ev{color:#f5d554;font-weight:600;margin-left:auto}

.marks-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;align-items:center;justify-items:center}
.mark-icon{width:46px;height:46px;background:var(--inset);border:1px solid var(--line);border-radius:7px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:10px;color:var(--muted)}
.mark-icon .lbl{font-size:9px;color:var(--muted-2)}

.skillgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.sk{width:100%;height:74px;background:var(--inset);border:1px solid var(--line);border-radius:8px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:11px;color:var(--muted);
  min-width:0;overflow:hidden;transition:border-color .15s,background .15s}
.sk .pp{font-size:10px;color:var(--muted-2);margin-top:2px}
.sk.sk5{grid-column:span 2}
.sk-click{cursor:pointer}
.sk-click:hover{border-color:var(--accent);background:var(--accent-soft)}
.sk-nm{color:var(--text);font-size:12px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sk-sub{color:var(--muted);font-size:10px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sk-attr{margin-right:4px}
.sk-pow{color:var(--amber)}

.soulmarks{display:none}
.smark-wrap{width:100%}
.smark-btn{width:100%;height:74px;background:var(--inset);border:1px solid var(--line);border-radius:8px;
  display:flex;align-items:center;gap:10px;padding:0 12px;cursor:pointer;color:var(--text);transition:border-color .15s,background .15s}
.smark-btn:hover{border-color:var(--accent);background:var(--accent-soft)}
.smark-img{width:44px;height:44px;object-fit:contain;background:var(--inset);border:1px solid var(--line);
  border-radius:7px;flex:0 0 auto;image-rendering:pixelated}
.smark-mid{display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-width:0}
.smark-title{font-size:15px;color:var(--text);line-height:1}
.smark-tags{display:flex;flex-wrap:wrap}
.smark-btn-disabled{width:100%;height:74px;background:var(--inset);border:1px dashed var(--line-2);border-radius:8px;
  display:flex;align-items:center;justify-content:center;color:var(--muted-2);font-size:12px}
.soulmark-tag{display:inline-block;background:#123054;color:var(--accent);border:1px solid #1f3d63;
  border-radius:4px;font-size:10px;padding:2px 7px;margin-right:4px}
.smark-modal-icon{width:56px;height:56px;margin:0 auto 10px}
.smark-modal-icon img{width:56px;height:56px;object-fit:contain;image-rendering:pixelated}
.smark-nav-btn{background:var(--elev);border:1px solid var(--line);color:var(--text);border-radius:7px;
  padding:5px 14px;font-size:12px;cursor:pointer;transition:background .15s,border-color .15s}
.smark-nav-btn:hover:not(:disabled){border-color:var(--accent);color:var(--accent);background:var(--elev-2)}
.smark-nav-btn:disabled{opacity:.4;cursor:default}

#war-list{overflow-y:auto;max-height:calc(100vh - 300px);min-height:240px}
#war-info{margin-top:12px;border-top:1px solid var(--line);padding-top:10px;font-size:12.5px;color:var(--text)}
.warfilt{background:var(--elev);border:1px solid var(--line);color:var(--muted);margin:0;padding:6px 14px;
  font-size:12px;border-radius:999px;font-weight:600;transition:background .15s,border-color .15s,color .15s}
.warfilt.active{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}

/* ---- 主题切换按钮 (白天/夜间) ---- */
.theme-btn{margin:0 0 0 auto;width:36px;height:36px;padding:0;border-radius:999px;
  border:1px solid var(--line);background:var(--elev);color:var(--text);display:inline-flex;
  align-items:center;justify-content:center;cursor:pointer;transition:background .15s,border-color .15s,color .15s,transform .06s}
.theme-btn:hover{background:var(--elev-2);border-color:var(--line-2)}
.theme-btn:active{transform:translateY(1px)}
.theme-btn svg{width:18px;height:18px}
.theme-btn .ic-moon{display:none}
[data-theme="light"] .theme-btn .ic-sun{display:none}
[data-theme="light"] .theme-btn .ic-moon{display:inline}

/* ---- 浅色(白天)主题 ---- */
:root[data-theme="light"]{
  --bg:#eef2f9; --panel:#ffffff; --card:#ffffff; --card-2:#eef2f8;
  --elev:#e9eef6; --elev-2:#dbe3f0; --inset:#f6f8fc; --inset-2:#eef2f8;
  --line:#d5dde9; --line-2:#b6c1d6;
  --text:#1f2a3d; --muted:#536179; --muted-2:#8792a7;
  --accent:#2f6fe4; --accent-deep:#2456b8; --accent-soft:#e2ebfb;
  --green:#1a9e5f; --green-deep:#147c4a;
  --amber:#b7791f; --red:#d14343; --purple:#7c53e0; --cyan:#0e7aa6; --btn-fg:#ffffff;
}
[data-theme="light"] body{background-image:radial-gradient(1100px 520px at 15% -10%,#e7edf8 0,rgba(231,237,248,0) 55%),
  radial-gradient(900px 480px at 100% 0,rgba(47,111,228,.06) 0,rgba(47,111,228,0) 60%)}
[data-theme="light"] header{background:linear-gradient(180deg,#ffffff,#f3f6fb);border-bottom:1px solid var(--line);
  box-shadow:0 1px 0 rgba(0,0,0,.03),0 4px 12px rgba(0,0,0,.06)}
[data-theme="light"] .st-idle{color:#98a2b3}
[data-theme="light"] .soulmark-tag{background:#e2ebfb;border-color:#b8cfe8;color:#2456b8}
[data-theme="light"] .abcell .ev{color:#a8790a}
[data-theme="light"] ::-webkit-scrollbar-thumb{background:#c6cfdd;border-color:var(--inset)}
[data-theme="light"] ::-webkit-scrollbar-thumb:hover{background:#aab6c9}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">S</div>
    <div>
      <h1>赛尔号协议调试台</h1>
      <div class="sub">PySeer · 协议调试与对战控制台</div>
    </div>
  </div>
  <button id="cbTheme" class="theme-btn" type="button" title="切换白天/夜间模式" aria-label="切换主题">
    <svg class="ic-sun" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
    <svg class="ic-moon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
  </button>
  <div class="status" id="status">idle</div>
</header>
<div class="tabs">
  <button class="tab active" data-tab="login">登录</button>
  <button class="tab" data-tab="bag">背包</button>
  <button class="tab" data-tab="scripts">脚本</button>
  <button class="tab" data-tab="battle">对战</button>
</div>
<div id="tab-login" class="tab-panel active">
<main>
  <div class="card" style="flex:1.2;max-width:560px">
    <h2>① 登录操作</h2>
    <label>米米号(账号)<sup style="color:var(--red)">*</sup> <span style="color:var(--muted)">可下拉选择已保存的</span></label>
    <input id="account" type="text" list="credList" placeholder="输入 或 选择已保存的米米号" autofocus>
    <datalist id="credList"></datalist>
    <label>密码<sup style="color:var(--red)">*</sup></label><input id="password" type="password">
    <div class="rowflex">
      <div><label>游戏服IP</label><input id="host" type="text" value="101.43.19.60"></div>
      <div><label>端口</label><input id="port" type="number" value="1201"></div>
    </div>
    <button id="loginBtn">登录</button><button id="discBtn" class="off" disabled>断开</button>
    <button id="delCredBtn" class="off" style="display:none">删除已选账号</button>
  </div>
</main>
</div><!-- /tab-login -->

<div id="tab-bag" class="tab-panel">
  <div class="bagwrap">
    <div class="card" style="flex:0 0 auto;width:314px">
      <h2>出战背包（第一背包）</h2><div id="bag-first" class="avrow"><div style="color:var(--muted)">等待...（登录后自动 43706 查询）</div></div>
      <h2>待命背包（第二背包）</h2><div id="bag-second" class="avrow"><div style="color:var(--muted)">等待...</div></div>
      <button id="teamBtn" class="off" title="查看并切换到其它阵容">切换阵容</button>
      <button id="wareBtn" class="off" title="打开/关闭精灵仓库">精灵仓库</button>
      <div id="bag-status" style="display:none"></div>
    </div>
    <div class="card" style="flex:1.5;min-width:360px">
      <div id="bag-detail-card">
        <h2 id="bag-title">精灵详情</h2>
        <div id="bag-detail"><div style="color:var(--muted)">选中一只精灵查看信息</div></div>
        <button id="storeBtn" class="off" disabled title="将选中精灵放入仓库">入库</button>
      </div>
      <div id="warehouse-view" style="display:none">
        <h2 id="war-title">精灵仓库</h2>
        <div style="display:flex;gap:8px;align-items:center;margin:0 0 8px;flex-wrap:wrap">
          <button id="warTypeNormal" class="warfilt active">普通仓库</button>
          <button id="warTypeExe" class="warfilt">精英仓库</button>
          <input id="warSearch" type="text" spellcheck="false" placeholder="按id搜索仓库精灵" style="flex:1;min-width:120px;padding:6px 8px;background:var(--inset);border:1px solid var(--line);color:var(--text);border-radius:6px;font:12px Menlo,monospace">
          <button id="warClose" class="off" style="display:none;margin:0;padding:2px 10px;font-size:12px" title="关闭搜索">×</button>
        </div>
        <div id="war-list" class="avrow"><div style="color:var(--muted)">点击“精灵仓库”后拉取...</div></div>
        <div id="war-info"></div>
      </div>
    </div>
  </div>
</div>

<div id="teamModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:999">
  <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:440px;max-height:72vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px">
    <h3 style="margin:0 0 8px;color:var(--accent)">阵容列表 <span id="teamNote" style="font-size:11px;color:var(--muted)"></span></h3>
    <div id="teamList"></div>
  </div>
</div>

<div id="skillModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000">
  <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:420px;max-height:76vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px">
    <h3 style="margin:0 0 10px;color:var(--accent)" id="skillModalTitle">技能详情</h3>
    <div id="skillModalBody" style="font-size:12px;color:var(--text)"></div>
  </div>
</div>

<div id="tab-scripts" class="tab-panel">
  <div style="display:flex;gap:12px;padding:12px;align-items:flex-start;flex-wrap:nowrap">
    <!-- 左半: 脚本列表 + 发包测试 -->
    <div style="flex:1;min-width:320px;display:flex;flex-direction:column;gap:12px">
      <div class="card">
        <h2>脚本 <button id="scriptRefreshBtn" class="off" style="float:right;margin:0;padding:2px 10px;font-size:12px">刷新</button></h2>
        <div id="scriptDir" style="font-size:11px;color:var(--muted);margin:0 0 8px;word-break:break-all">—</div>
        <div id="scriptList" style="max-height:30vh;overflow:auto;border:1px solid var(--line);border-radius:6px;background:var(--inset)">
          <div style="color:var(--muted);padding:8px">等待加载...</div>
        </div>
        <button id="scriptRunBtn" disabled>运行选中脚本</button>
        <button id="scriptStopBtn" class="off" style="display:none">停止脚本</button>
        <div id="scriptStatus" style="font-size:12px;color:var(--muted);margin-top:6px">—</div>
      </div>
      <div class="card">
        <h2>③ 发包测试</h2>
        <label>命令（全部）<sup style="color:var(--muted-2)">选一个跳到下面</sup></label>
        <select id="cmdRef"><option value="">— 从全部 2910 条命令中选择 —</option></select>
        <label>命令号 / 名字（可输入过滤）</label><input id="cmd" type="text" list="cmdList" placeholder="如 40001 或 ENTER_MAP" value="40001">
        <datalist id="cmdList"></datalist>
        <label>包体参数（十进制，逗号/空格分隔）<sup style="color:var(--muted-2)">自动转标准包体</sup></label>
        <input id="body" type="text" placeholder="十进制参数, 逗号/空格分隔; 可留空(空包体)。如 0 10 725 172  → 000000000000000A000002D5000000A0" value="">
        <div class="rowflex" style="align-items:center;gap:8px">
          <label style="margin:0"><input id="rawHex" type="checkbox"> 原样HEX</label>
          <div style="margin-left:auto"><label style="margin:0">预览</label><code id="bodyPrev" style="display:inline-block;margin-left:6px;padding:2px 6px;background:var(--inset);border:1px solid var(--line);border-radius:4px;color:var(--cyan);font:11px Menlo,monospace;word-break:break-all">—</code></div>
        </div>
        <button id="sendBtn" disabled>发送</button>
        <button id="petbagBtn" class="off" disabled title="发送命令 43706 GET_PET_INFO_BY_ONCE, 查询背包内所有精灵(含能力值)">查询背包精灵(43706)</button>
        <div style="margin-top:8px"><label>服务器响应（实时，内容可选中复制；受过滤包id/收发复选框约束）</label>
          <div id="sendStatus" style="font-size:12px;color:var(--muted);margin:2px 0 6px">—</div>
          <div id="pktWrap" style="max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:6px;background:var(--inset)">
            <table id="pktTable" style="width:100%;table-layout:fixed;border-collapse:collapse;font:12px Menlo,monospace;user-select:text;cursor:text">
              <colgroup>
                <col style="width:60px">
                <col style="width:84px">
                <col style="width:26%">
                <col>
              </colgroup>
              <thead>
                <tr style="text-align:left;background:var(--card)">
                  <th style="padding:4px 8px;border:1px solid var(--line)">类型</th>
                  <th style="padding:4px 8px;border:1px solid var(--line)">命令号</th>
                  <th style="padding:4px 8px;border:1px solid var(--line)">包体(hex)</th>
                  <th style="padding:4px 8px;border:1px solid var(--line)">十进制数组</th>
                </tr>
              </thead>
              <tbody id="pktBody"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    <!-- 右半: 脚本输出 (控制台) + 日志输出 -->
    <div style="flex:1.6;min-width:420px;display:flex;flex-direction:column;gap:12px">
      <div class="card">
        <h2>脚本输出 (实时) <button id="scriptClearBtn" class="off" style="float:right;margin:0;padding:2px 10px;font-size:12px">清空</button></h2>
        <div id="scriptOutput" style="max-height:34vh;overflow:auto;background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:8px;font:12px/1.5 Menlo,monospace;white-space:pre-wrap;color:var(--green)">（尚未运行脚本; 运行后 print 输出会实时显示在这里）</div>
      </div>
      <div class="card">
        <h2>② 日志输出 (实时) <button id="clearLogBtn" class="off" style="float:right;margin:0;padding:2px 10px;font-size:12px">清空输出</button></h2>
        <div class="filterbar">
          <label style="margin:0">过滤包id</label>
          <input id="filterIds" type="text" spellcheck="false" placeholder="逗号分隔, 如 40002,2192,41228,4047,4475,41080,9134,2604,9019,2101,2004,3405,2601,2002,43321,1002,9908">
          <label style="margin:0"><input id="chkSend" type="checkbox" checked> 接收send</label>
          <label style="margin:0"><input id="chkRecv" type="checkbox" checked> 接收recv</label>
          <button id="applyFilterBtn" class="off" style="margin:0;padding:2px 10px;font-size:12px">应用过滤</button>
        </div>
        <div id="log" style="max-height:34vh"></div>
      </div>
    </div>
  </div>
</div><!-- /tab-scripts -->

<div id="tab-battle" class="tab-panel">
  <!-- 发起对战: 输入带cmdid的完整HEX包 (任意命令号) -->
  <div style="padding:12px">
    <div class="card">
      <h2>发起对战 (输入带 cmdid 的完整 HEX 包 <sup style="color:var(--muted-2)">支持任意对战命令号, 不限于 41129</sup>)</h2>
      <div class="rowflex" style="align-items:center;gap:8px">
        <input id="battleHex" type="text" spellcheck="false" placeholder="粘贴完整十六进制包, 如 00000015310000A0A9383934A300000255000030A0">
        <button id="battleHexBtn" disabled>发送</button>
        <button id="battleClearBtn" class="off">清空对战状态</button>
      </div>
      <div id="battleHexInfo" style="font-size:12px;color:var(--muted);margin-top:4px">—</div>
    </div>
  </div>
  <!-- 对战信息区: 我方/敌方 各占一列, 头像+血条 + 各自出场队伍 -->
  <div style="padding:0 12px 12px">
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      <div class="card" style="flex:1;min-width:300px">
        <h2>我方</h2>
        <div id="battleMy" class="fight-side"><div style="color:var(--muted)">等待对战包(2503/2504)...</div></div>
        <h2 style="margin-top:10px">我方出场队伍</h2>
        <div id="battleMyTeam" class="battle-team"><div style="color:var(--muted)">—</div></div>
      </div>
      <div class="card" style="flex:1;min-width:300px">
        <h2>敌方</h2>
        <div id="battleOther" class="fight-side"><div style="color:var(--muted)">等待对战包...</div></div>
        <h2 style="margin-top:10px">敌方出场队伍</h2>
        <div id="battleOtherTeam" class="battle-team"><div style="color:var(--muted)">—</div></div>
      </div>
    </div>
    <!-- 操作区: 单列, 位于对战信息区下方 -->
    <div class="card" style="margin-top:12px">
      <h2>操作区</h2>
      <div style="font-size:12px;color:var(--muted);margin:4px 0 6px">技能 (点击发 2405 USE_SKILL)</div>
      <div id="battleSkills" style="display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 8px"><div style="color:var(--muted)">—</div></div>
      <div id="battleOps" style="display:flex;flex-wrap:wrap;gap:8px"></div>
      <div id="battleActionStatus" style="font-size:12px;color:var(--muted);margin-top:6px">—</div>
      <div id="battleChangePetPicker" style="display:none;margin-top:8px;border:1px solid var(--line);background:var(--inset);border-radius:8px;padding:8px"></div>
    </div>
  </div>
  <!-- 战报记录 -->
  <div style="padding:0 12px 12px">
    <div class="card">
      <h2>战报记录 <button id="battleReportCopyBtn" class="off" style="float:right;margin:0 6px 0 0;padding:2px 10px;font-size:12px">复制</button><button id="battleReportClearBtn" class="off" style="float:right;margin:0;padding:2px 10px;font-size:12px">清空</button></h2>
      <div id="battleReport" style="max-height:32vh;overflow:auto;background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:8px;font:12px/1.5 Menlo,monospace;white-space:pre-wrap;color:var(--cyan)">（对战中逐回合记录: 技能/换宠/HP变化/道具/捕捉/结束）</div>
    </div>
  </div>
</div><!-- /tab-battle -->

<script>
const logEl=document.getElementById('log');
const statusEl=document.getElementById('status');
function setStatus(s){statusEl.textContent=s;statusEl.className='status st-'+s;}

// ---- 主题切换 (白天/夜间) ----
let _theme = document.documentElement.getAttribute('data-theme') || 'dark';
function applyTheme(t){
  _theme = t;
  document.documentElement.setAttribute('data-theme', t);
  try{ localStorage.setItem('pyseer_theme', t); }catch(e){}
}
document.getElementById('cbTheme').addEventListener('click',()=>{
  applyTheme(_theme==='light' ? 'dark' : 'light');
});


// ---- 过滤/接收开关 (前端显示层) ----
let idFilter = new Set();
const chkSendEl = document.getElementById('chkSend');
const chkRecvEl = document.getElementById('chkRecv');
const filterIdsEl = document.getElementById('filterIds');
function shouldShow(e){
  if(e && e.level==='script') return false;               // 脚本输出只显示在"脚本输出"控制台, 不混进封包日志
  if(e && e.direction){                       // 封包
    const c = Number(e.cmd);
    if(Number.isFinite(c) && idFilter.has(c)) return false;     // 名单内的包舍弃
    const d = String(e.direction).toUpperCase();
    if(d==='SEND' && !chkSendEl.checked) return false;
    if(d==='RECV' && !chkRecvEl.checked) return false;
  }
  return true;
}

// ---- 脚本输出控制台 (print/stdout 显示在这里) ----
const scriptOutEl=document.getElementById('scriptOutput');
function appendScriptOutput(e){
  const line=document.createElement('div');
  line.textContent=(e.msg||'');
  scriptOutEl.appendChild(line);
  scriptOutEl.scrollTop=scriptOutEl.scrollHeight;
  while(scriptOutEl.childNodes.length>5000) scriptOutEl.removeChild(scriptOutEl.firstChild);
}

let lastSeenSeq=0;   // 仅用于去重 SSE 断线重连的回放, 不限制新包
function appendLog(e){
  const line=document.createElement('div');
  line.className='lvl-'+(e.level||'info');
  let t=`${e.t} [${e.level}] ${e.msg}`;
  if(e.direction) t=`${e.t} [${e.direction}] cmd=${e.cmd}  ${e.msg}`;
  line.textContent=t;
  logEl.appendChild(line);
  logEl.scrollTop=logEl.scrollHeight;
  while(logEl.childNodes.length>20000) logEl.removeChild(logEl.firstChild);
}

// 实时接收: SSE 推送 (取代原来的 1.2s 轮询; 去掉 seq 去重限制)
const es = new EventSource('/api/stream');
es.onmessage = (ev)=>{
  let e; try{ e=JSON.parse(ev.data); }catch(_){ return; }
  if(e.seq && e.seq<=lastSeenSeq) return;                 // 去重断线回放
  if(e.level==='script'){ appendScriptOutput(e); }        // 脚本输出 -> 专用控制台
  else if(e.level==='battle'){ appendLog(e); refreshBattle(); }   // 后台检测到对战行为 -> 显示提示并自动切到对战界面
  else if(shouldShow(e)){ appendLog(e); if(e.direction) appendTable(e); }
  if(e.seq && e.seq>lastSeenSeq) lastSeenSeq=e.seq;
};
es.onerror = ()=>{};                                        // 浏览器会自动重连

// 从 /api/log 整panel重建 (应用当前过滤/开关); 用于初始加载与改动过滤/开关后即时生效
async function renderLog(){
  try{
    const r=await fetch('/api/log'); const logs=await r.json();
    logEl.innerHTML=''; scriptOutEl.innerHTML=''; lastSeenSeq=0;
    for(const e of logs){
      if(e.level==='script'){ appendScriptOutput(e); }
      else if(shouldShow(e)){ appendLog(e); }
      if(e.seq && e.seq>lastSeenSeq) lastSeenSeq=e.seq;
    }
  }catch(e){}
}

// 读取 / 保存过滤名单
async function loadFilter(){
  try{
    const r=await fetch('/api/filter'); const j=await r.json();
    idFilter=new Set((j.ids||[]).map(Number));
    filterIdsEl.value=(j.ids||[]).join(', ');
  }catch(e){}
}
async function saveFilter(){
  const raw=filterIdsEl.value;
  const ids=raw.split(/[,\s]+/).map(s=>Number(s.trim())).filter(n=>Number.isFinite(n));
  try{
    const r=await fetch('/api/filter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
    const j=await r.json(); idFilter=new Set((j.ids||[]).map(Number));
  }catch(e){}
  renderLog(); renderTable();
}
function clearOutput(){ logEl.innerHTML=''; }

document.getElementById('applyFilterBtn').onclick=saveFilter;
chkSendEl.addEventListener('change',()=>{renderLog();renderTable();});
chkRecvEl.addEventListener('change',()=>{renderLog();renderTable();});
loadFilter().then(renderLog);
// 记住的账号密码 (与"米米号"字段合并: 下拉选择已保存米米号 -> 自动填账号+密码)
let savedAccounts=[];
async function loadCreds(){
  try{
    const r=await fetch('/api/credentials'); const j=await r.json(); savedAccounts=j.accounts||[];
    // 填充"米米号"字段的 datalist (已保存的米米号)
    const dl=document.getElementById('credList');
    dl.innerHTML='';
    savedAccounts.forEach(e=>{ const o=document.createElement('option'); o.value=e.account; dl.appendChild(o); });
    const delBtn=document.getElementById('delCredBtn');
    if(savedAccounts.length){
      const last=savedAccounts[savedAccounts.length-1];
      document.getElementById('account').value=last.account;
      document.getElementById('password').value=last.password;
      delBtn.style.display='inline-block';
    }else{ delBtn.style.display='none'; }
  }catch(e){}
}
// 在"米米号"里选中(或输入)某个已保存的米米号 => 自动填入密码
document.getElementById('account').addEventListener('input',()=>{
  const acc=document.getElementById('account').value.trim();
  const hit=savedAccounts.find(e=>e.account===acc);
  if(hit) document.getElementById('password').value=hit.password;
});
document.getElementById('delCredBtn').onclick=async()=>{
  const acc=document.getElementById('account').value.trim();
  if(!acc){ appendLog({t:now(),level:'tip',msg:'请先填写要删除的米米号(账号)'}); return; }
  const r=await fetch('/api/credentials/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:acc})});
  const j=await r.json();
  if(j.ok){ savedAccounts=j.accounts; document.getElementById('account').value=''; document.getElementById('password').value=''; loadCreds(); }
};
loadCreds();

let _prevStatus='idle';
async function refreshStatus(){
  try{const r=await fetch('/api/status');const s=await r.json();
    setStatus(s.status||'idle');
    document.getElementById('loginBtn').disabled=(s.status==='logging_in');
    document.getElementById('sendBtn').disabled=(s.status!=='ready');
    document.getElementById('petbagBtn').disabled=(s.status!=='ready');
    document.getElementById('teamBtn').disabled=(s.status!=='ready');
    document.getElementById('wareBtn').disabled=(s.status!=='ready');
    document.getElementById('discBtn').disabled=(s.status==='idle');
    if(s.status==='ready' && _prevStatus!=='ready'){   // 登录完成 -> 自动跳到"脚本"页
      activateTab('scripts');
    }
    _prevStatus=s.status||'idle';
  }catch(e){}
}
setInterval(refreshStatus,1000);refreshStatus();

function now(){return new Date().toTimeString().slice(0,8);}
document.getElementById('loginBtn').onclick=async()=>{
  const btn=document.getElementById('loginBtn');
  const accEl=document.getElementById('account');
  const pwdEl=document.getElementById('password');
  const acc=accEl.value.trim(), pwd=pwdEl.value;
  accEl.style.border=''; pwdEl.style.border='';
  if(!acc){accEl.focus();accEl.style.border='1px solid var(--red)';setStatus('error');appendLog({t:now(),level:'error',msg:'请先填写米米号(账号)'});return;}
  if(!pwd){pwdEl.focus();pwdEl.style.border='1px solid var(--red)';setStatus('error');appendLog({t:now(),level:'error',msg:'请先填写密码'});return;}
  btn.disabled=true;
  try{
    const body={account:acc,password:pwd,
      host:document.getElementById('host').value,port:document.getElementById('port').value};
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(!j.ok){setStatus('error');appendLog({t:now(),level:'error',msg:'登录请求失败: '+(j.error||('HTTP '+r.status))});btn.disabled=false;}
    else{setStatus('logging_in');appendLog({t:now(),level:'info',msg:'登录请求已提交, 后台处理中...'});}
  }catch(e){
    setStatus('error');
    appendLog({t:now(),level:'error',msg:'提交登录出错: '+e});
    btn.disabled=false;
  }
};
document.getElementById('discBtn').onclick=async()=>{await fetch('/api/disconnect',{method:'POST'});refreshStatus();renderLog();};
document.getElementById('clearLogBtn').onclick=clearOutput;
document.getElementById('sendBtn').onclick=async()=>{
  const body={cmd:document.getElementById('cmd').value,body:document.getElementById('body').value,
    encode:document.getElementById('rawHex').checked?'hex':'pack'};
  const st=document.getElementById('sendStatus');
  try{
    const r=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(j.ok){
      const n=(window.__CM||{})[j.sent.cmd]||'';
      st.textContent=`已发送 cmd=${j.sent.cmd} ${n}　(不阻塞等待应答, 实时显示在下方表格)`;
      st.style.color='var(--green)';
    }else{ st.textContent='发送失败: '+(j.error||''); st.style.color='var(--red)'; }
  }catch(e){ st.textContent='发送出错: '+e; st.style.color='var(--red)'; }
};
// 查询背包精灵: 发送命令 43706 GET_PET_INFO_BY_ONCE (空包体), 由后台监听线程读取应答
document.getElementById('petbagBtn').onclick=async()=>{
  const st=document.getElementById('sendStatus');
  st.textContent='正在发送 43706 GET_PET_INFO_BY_ONCE 查询背包精灵...'; st.style.color='var(--amber)';
  const body={cmd:'43706',body:'',encode:'pack'};
  try{
    const r=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(j.ok){
      st.textContent='已发送 43706 GET_PET_INFO_BY_ONCE (查询背包一切精灵), 应答实时显示在下方表格 (RECV)';
      st.style.color='var(--green)';
    }else{ st.textContent='发送失败: '+(j.error||''); st.style.color='var(--red)'; }
  }catch(e){ st.textContent='发送出错: '+e; st.style.color='var(--red)'; }
};
// ---- 服务器响应表格 (实时, 可选中复制) ----
const pktBodyEl=document.getElementById('pktBody');
let pktRows=[];              // 所有已记表封包 (供收发复选框/过滤重渲染)
const PKT_MAX=3000;
function renderTableRow(r){
  const tr=document.createElement('tr');
  // 类型列: SEND / RECV 单行显示; 其余列单行 + 超出省略号, 但 title 保存完整内容便于复制
  const mkCell=(val,color)=>{ const cd=document.createElement('td'); cd.textContent=val; cd.title=val;
    cd.style.padding='3px 8px'; cd.style.border='1px solid var(--elev)'; cd.style.color=color || 'var(--text)';
    cd.style.whiteSpace='nowrap'; cd.style.overflow='hidden'; cd.style.textOverflow='ellipsis';
    return cd; };
  const c1=mkCell(r.dir, r.dir==='SEND'?'var(--cyan)':'var(--green)');
  const c2=mkCell(r.cmd);
  const c3=mkCell(r.body);
  const c4=mkCell((r.ints&&r.ints.length)?`[${r.ints.join(', ')}]`:(r.body?'(非int32)':'[]'));
  tr.appendChild(c1);tr.appendChild(c2);tr.appendChild(c3);tr.appendChild(c4);
  return tr;
}
function appendTable(e){
  // 归一化入口: SSE 日志项里的方向字段是 direction, 表格行统一用 dir
  const r={dir:e.direction, cmd:e.cmd, body:e.body, ints:e.ints||[]};
  pktRows.push(r);
  if(pktRows.length>PKT_MAX) pktRows.shift();
  pktBodyEl.appendChild(renderTableRow(r));
  while(pktBodyEl.childNodes.length>PKT_MAX) pktBodyEl.removeChild(pktBodyEl.firstChild);
  const wrap=document.getElementById('pktWrap'); if(wrap) wrap.scrollTop=wrap.scrollHeight;
}
function renderTable(){          // 应用当前收发复选框 + 包id过滤, 重渲染整表
  pktBodyEl.innerHTML='';
  for(const r of pktRows){
    const c=Number(r.cmd);
    if(Number.isFinite(c) && idFilter.has(c)) continue;
    const d=String(r.dir).toUpperCase();
    if(d==='SEND' && !chkSendEl.checked) continue;
    if(d==='RECV' && !chkRecvEl.checked) continue;
    pktBodyEl.appendChild(renderTableRow(r));
  }
}
// 包体实时预览: 把参数列表打包成标准包体并显示十六进制
const bodyEl=document.getElementById('body'), rawEl=document.getElementById('rawHex'), prevEl=document.getElementById('bodyPrev');
bodyEl.addEventListener('input',updateBodyPreview);
rawEl.addEventListener('change',updateBodyPreview);
async function updateBodyPreview(){
  const spec=bodyEl.value, raw=rawEl.checked;
  prevEl.textContent='…';
  try{
    if(raw){ prevEl.textContent=(spec||'(空)'); return; }
    const r=await fetch('/api/body-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({spec})});
    const j=await r.json();
    if(j.ok){
      const parts=j.parts||[];
      prevEl.textContent=j.length?`${j.hex}  (${j.length}B; ${parts.map(p=>p[0]+'='+p[2]+'B').join(', ')}| 分包: ${parts.map(p=>p[0]).join(', ')})`:'(空包体)';
      prevEl.style.color='var(--cyan)';
    }else{ prevEl.textContent='错误: '+(j.error||''); prevEl.style.color='var(--red)'; }
  }catch(e){ prevEl.textContent='错误: '+e; prevEl.style.color='var(--red)'; }
}
updateBodyPreview();
// 命令名自动补全 + 应答显示命令名
window.__CM={};
async function loadCmdMap(){
  try{ const r=await fetch('/api/cmdmap'); const m=await r.json(); window.__CM=m;
    const dl=document.getElementById('cmdList'); dl.innerHTML='';
    const ref=document.getElementById('cmdRef'); ref.innerHTML='<option value="">— 从全部 '+Object.keys(m).length+' 条命令中选择 —</option>';
    const opts=[];
    for(const [id,name] of Object.entries(m)){ opts.push(name+' ('+id+')'); }
    opts.sort();
    opts.forEach(o=>{
      const d=document.createElement('option'); d.value=o; dl.appendChild(d);
      const s=document.createElement('option');
      s.value=o.split(' (')[0];           // 选中后填入命令名
      s.textContent=o;                    // 显示 name (id)
      ref.appendChild(s);
    });
  }catch(e){}
}
// 在"命令(全部)"下拉里选一个 => 填入命令号输入框, 便于直接发送
document.getElementById('cmdRef').onchange=()=>{
  const v=document.getElementById('cmdRef').value;
  if(v) document.getElementById('cmd').value=v;
};
loadCmdMap();

// ---- 分页切换 ----
function activateTab(name){
  document.querySelectorAll('.tabs .tab').forEach(x=>x.classList.remove('active'));
  const tb=document.querySelector(`.tabs .tab[data-tab="${name}"]`); if(tb) tb.classList.add('active');
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  const p=document.getElementById('tab-'+name); if(p) p.classList.add('active');
  if(name==='bag') refreshBag();
  if(name==='scripts') loadScripts();
  if(name==='battle') refreshBattle();
}
document.querySelectorAll('.tabs .tab').forEach(t=>{
  t.addEventListener('click',()=>activateTab(t.dataset.tab));
});

// ---- "脚本"页: 列出默认目录脚本, 可选择运行 ----
let scriptsList=[];
const scriptRunBtn=document.getElementById('scriptRunBtn');
const scriptStopBtn=document.getElementById('scriptStopBtn');
const scriptStatusEl=document.getElementById('scriptStatus');
async function loadScripts(){
  try{
    const r=await fetch('/api/scripts'); const j=await r.json();
    document.getElementById('scriptDir').textContent=j.dir||'—';
    scriptsList=j.scripts||[];
    const box=document.getElementById('scriptList');
    box.innerHTML='';
    if(!scriptsList.length){
      const d=document.createElement('div'); d.style.cssText='color:var(--muted);padding:8px';
      d.textContent='（该目录暂无脚本, 把 .py 脚本放进上面所示的目录即可）'; box.appendChild(d);
    }
    scriptsList.forEach(nm=>{
      const b=document.createElement('div');
      b.className='script-item'; b.textContent=nm; b.dataset.name=nm;
      b.onclick=()=>{
        document.querySelectorAll('#scriptList .script-item').forEach(x=>x.classList.remove('sel'));
        b.classList.add('sel');
        scriptRunBtn.disabled=false;
        scriptRunBtn.dataset.name=nm;
        scriptStatusEl.textContent='已选择: '+nm; scriptStatusEl.style.color='var(--muted)';
      };
      box.appendChild(b);
    });
    if(j.running){ scriptStopBtn.style.display='inline-block'; }
  }catch(e){}
}
document.getElementById('scriptRefreshBtn').onclick=loadScripts;
scriptRunBtn.onclick=async()=>{
  const nm=scriptRunBtn.dataset.name;
  if(!nm){ appendLog({t:now(),level:'tip',msg:'请先选择要运行的脚本'}); return; }
  scriptOutEl.innerHTML='';   // 每个脚本运行前清空输出控制台, 只显示本次运行
  scriptStatusEl.textContent='正在启动 '+nm+' ...'; scriptStatusEl.style.color='var(--amber)';
  try{
    const r=await fetch('/api/scripts/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nm})});
    const j=await r.json();
    if(j.ok){ scriptStatusEl.textContent='已启动 '+nm; scriptStatusEl.style.color='var(--green)'; scriptStopBtn.style.display='inline-block'; }
    else{ scriptStatusEl.textContent='启动失败: '+(j.error||''); scriptStatusEl.style.color='var(--red)'; scriptStopBtn.style.display='none'; }
  }catch(e){ scriptStatusEl.textContent='启动出错: '+e; scriptStatusEl.style.color='var(--red)'; }
};
scriptStopBtn.onclick=async()=>{
  try{ await fetch('/api/scripts/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
};
document.getElementById('scriptClearBtn').onclick=()=>{ scriptOutEl.innerHTML=''; };
loadScripts();   // 预加载脚本列表 (登录后可立即看到)


// ---- 对战页: 轮询 /api/battle, 图形化双方头像/血量/参数 + 技能/操作按钮 + 发起HEX包 ----
// 自动切换: 后台一监听到对战行为(2503 开始), 就自动切到"对战"界面并开始监听展示对战流程.
let _battleAutoOn = false;   // 本场对战是否已自动切到对战界面(避免重复切换/打扰用户)
function battleTabLive(live){
  const tb=document.querySelector('.tabs .tab[data-tab="battle"]');
  if(tb) tb.classList.toggle('live', !!live);
}
function maybeOpenBattle(active, finished){
  battleTabLive(active && !finished);               // "对战"标签上显示绿色亮点 = 对战进行中
  if(active && !finished){                       // 对战进行中
    if(!_battleAutoOn){ _battleAutoOn = true; activateTab('battle'); }
  } else { _battleAutoOn = false; }              // 无对战 / 已结束 -> 复位, 下轮可再自动切
}
async function refreshBattle(){
  try{
    const r=await fetch('/api/battle'); const j=await r.json();
    renderBattleState(j);
    maybeOpenBattle(j.active, j.finished);
  }catch(e){}
}
function hpPct(hp,max){ if(hp==null||max==null||!max) return 0; return Math.max(0,Math.min(100,Math.round(hp*100/max))); }
function renderFighterBox(el, p){
  el.innerHTML='';
  if(!p){ el.innerHTML='<div style="color:var(--muted)">—</div>'; return; }
  const pid=p.id||p.petID;
  const card=document.createElement('div'); card.className='fight-card';
  const img=document.createElement('div'); img.className='fc-img';
  if(p.avatar){ const im=document.createElement('img'); im.src=p.avatar; im.alt=''; im.onerror=()=>{img.textContent=(p.name||pid||'?');}; img.appendChild(im); }
  else img.textContent=(p.name||pid||'?');
  card.appendChild(img);
  const nm=document.createElement('div'); nm.className='fc-name'; nm.textContent=(p.name||('id='+pid));
  card.appendChild(nm);
  const lv=document.createElement('div'); lv.className='fc-lv'; lv.textContent='Lv.'+(p.lv!=null?p.lv:'?')+'  catch='+(p.catchTime!=null?p.catchTime:'?');
  card.appendChild(lv);
  const hp=(p.hp!=null)?p.hp:p.xinHp, mhp=(p.maxHP!=null)?p.maxHP:p.xinMaxHp;
  const bar=document.createElement('div'); bar.className='hpbar';
  const f=document.createElement('div'); f.className='hp'+(hpPct(hp,mhp)<30?' low':''); f.style.width=hpPct(hp,mhp)+'%';
  bar.appendChild(f); card.appendChild(bar);
  const ht=document.createElement('div'); ht.className='hptxt'; ht.textContent=(hp!=null?hp:'?')+' / '+(mhp!=null?mhp:'?');
  card.appendChild(ht);
  const pg=document.createElement('div'); pg.className='fc-page';
  pg.textContent=(p.siteBuff&&p.siteBuff.siteBuffId)?('场地buff '+p.siteBuff.siteBuffId+' 回合'+p.siteBuff.siteBuffTurn):'';
  card.appendChild(pg);
  el.appendChild(card);
}
function renderTeamBox(el, team){
  el.innerHTML='';
  if(!team||!team.length){ el.innerHTML='<div style="color:var(--muted)">—</div>'; return; }
  for(const p of team){
    const d=document.createElement('div'); d.className='pt-chip';
    const im=document.createElement('img'); im.src=p.avatar||('/head/'+(p.id)+'.png'); im.onerror=()=>{im.style.display='none';};
    d.appendChild(im);
    const n=document.createElement('div'); n.className='pn'; n.textContent=(p.name||('id='+p.id));
    d.appendChild(n);
    const h=document.createElement('div'); h.className='ph'; h.textContent='HP '+(p.hp!=null?p.hp:'?');
    d.appendChild(h);
    el.appendChild(d);
  }
}
// 强制换宠相关状态: 是否已自动弹出过换宠(防重复)
let _forcedChangeOpened=false;
// 统一读取某只精灵的当前 HP (不同来源可能叫 hp 或 xinHp)
function _petHp(p){ return p ? ((p.hp!=null)? p.hp : p.xinHp) : null; }
// 判断是否处于"强制换宠": 我方当前精灵已阵亡(hp<=0) 且我方队伍里仍有存活替补
function _isForcedChange(j){
  if(!j || !j.active) return false;
  const my=j.my; if(!my) return false;
  const curHp=_petHp(my);
  if(curHp==null || curHp>0) return false;         // 当前精灵还活着 -> 无需强制换
  // 队伍里仍有 >0 血的精灵(且不是这只已阵亡的), 说明还能换 -> 强制换宠
  const team=j.myTeam||[];
  return team.some(p=> p.catchTime!==my.catchTime && _petHp(p) > 0);
}
// 锁定/恢复操作区: 禁用技能按钮 (ops 按钮由 renderBattleState 单独处理)
function _setBattleOpsLocked(locked){
  const sk=document.getElementById('battleSkills');
  if(!sk) return;
  Array.from(sk.querySelectorAll('button.skill-btn')).forEach(btn=>{
    btn.disabled = locked || btn.getAttribute('data-ppzero')==='1';
  });
}
function renderBattleState(j){
  if(!j) return;
  renderFighterBox(document.getElementById('battleMy'), j.my);
  renderFighterBox(document.getElementById('battleOther'), j.other);
  renderTeamBox(document.getElementById('battleMyTeam'), j.myTeam);
  renderTeamBox(document.getElementById('battleOtherTeam'), j.otherTeam);
  const skills=j.mySkills||[];
  window.__lastBattleSkills=skills;   // 供技能按钮 PP 重绘时取用
  renderSkillButtons(skills, j.mySkillPP);
  ensureSkillNames(skills);   // 拉取技能名, 加载后自动重渲染
  // ---- 强制换宠: 我方当前精灵阵亡(且仍有存活替补) 时锁定操作并弹出换宠 ----
  const forced = _isForcedChange(j);
  // 锁定其他操作(技能/非换宠按钮), 直到换宠完成
  _setBattleOpsLocked(forced);
  // 仅当"刚进入"强制状态时自动弹出一次换宠选择
  if(forced && !_forcedChangeOpened){
    _forcedChangeOpened=true;
    openChangePetPicker(j, true);
  } else if(!forced){
    // 强制已解除: 若换宠框是之前"强制"自动弹出的(且用户未在框里选定), 收起, 避免残留
    if(_forcedChangeOpened){
      const cpk=document.getElementById('battleChangePetPicker');
      if(cpk){ cpk.style.display='none'; cpk.innerHTML=''; }
    }
    _forcedChangeOpened=false;
  }
  const ops=document.getElementById('battleOps'); ops.innerHTML='';
  [['换宠(2407)',2407,true],['用药(2406)',2406,false],['逃跑(2410)',2410,false],['捕捉(2409)',2409,false]].forEach(([label,cmd,isChange])=>{
    const b=document.createElement('button'); b.className='ops-btn'; b.textContent=label;
    b.title='发送 cmd='+cmd;
    if(isChange){ b.onclick=()=>openChangePetPicker(j, forced); }
    else{ b.onclick=()=>sendBattleCmd(cmd,[]); }
    if(forced && !isChange) b.disabled=true;   // 强制换宠期间禁用非换宠操作
    ops.appendChild(b);
  });
  const bhi=document.getElementById('battleHexInfo');
  if(bhi) bhi.textContent=(j.active?('对战中 mode='+j.mode+' | 上次更新='+(j.lastCmd||'-')):'未在对战中');
  document.getElementById('battleHexBtn').disabled=!(j.client_present);
  // 未在对战中时收起换宠选择框 (避免残留)
  if(!j.active){
    const cpk=document.getElementById('battleChangePetPicker');
    if(cpk){ cpk.style.display='none'; cpk.innerHTML=''; }
    _forcedChangeOpened=false;
  }
  // 战报
  const rep=document.getElementById('battleReport');
  if(rep){
    const entries=j.report||[];
    // 从上到下(时间正序): 首条在最上, 新增追加到最下; 不强制滚动,
    // 仅当用户本就停在底部时才跟随新内容滚动(正常日志行为)
    const nearBottom=(rep.scrollTop+rep.clientHeight)>=(rep.scrollHeight-40);
    rep.innerHTML=entries.length? '' : '<div style="color:var(--muted)">（暂无战报记录）</div>';
    entries.forEach(e=>{
      const d=document.createElement('div');
      d.textContent=e.t+'  '+e.msg;
      rep.appendChild(d);
    });
    if(entries.length && nearBottom) rep.scrollTop=rep.scrollHeight;
  }
}
let _SKILL_NAMES={};   // sid -> 技能名 (由 /api/skills 拉取)
let _SKILL_DATA={};    // sid -> 技能完整数据 (power/pp 等)
let _SKILL_PP={};      // sid -> 当前可用 PP (优先用服务器下发的 mySkillPP, 无则按 maxpp)
function renderSkillButtons(skills, ppMap){
  const sk=document.getElementById('battleSkills'); sk.innerHTML='';
  if(!skills.length){ sk.innerHTML='<div style="color:var(--muted)">—</div>'; return; }
  // 记录服务器下发的当前 PP（dict: sid字符串->当前pp），供本函数与本地同步使用
  if(ppMap){ for(const k in ppMap){ _SKILL_PP[k]=ppMap[k]; } }
  // 五号位(第5个索引=4)技能移到最左侧; 其余按序
  const ordered = [...skills];
  if(ordered.length>=5){ const fifth=ordered.splice(4,1)[0]; ordered.unshift(fifth); }
  ordered.forEach(sid=>{
    const b=document.createElement('button'); b.className='skill-btn';
    const d=_SKILL_DATA[sid]||{};
    const name=_SKILL_NAMES[sid]!=null? _SKILL_NAMES[sid] : sid;
    const power=d.power!=null? d.power : '';       // 威力
    const maxp=d.pp!=null? d.pp : '';               // 最大 PP
    const curp=_SKILL_PP[sid]!=null? _SKILL_PP[sid] : (_SKILL_PP[String(sid)]!=null? _SKILL_PP[String(sid)] : maxp);   // 当前 PP
    // 名字
    const nm=document.createElement('div'); nm.className='sb-name'; nm.textContent=String(name);
    // 威力行
    const sub=document.createElement('div'); sub.className='sb-sub';
    sub.textContent=(power!==''?('威力'+power):'') + (d.typeName?(' · '+d.typeName):'');
    // PP 行
    const pp=document.createElement('div'); pp.className='sb-pp';
    pp.textContent=(maxp!==''?('PP '+(curp!==''?curp:'?')+'/'+maxp):'');
    b.appendChild(nm); b.appendChild(sub); b.appendChild(pp);
    b.title='技能ID='+sid+'  点击发送 USE_SKILL(2405)';
    const ppZero = (curp!=='' && curp<=0);
    b.setAttribute('data-ppzero', ppZero?'1':'0');   // 供强制换宠锁定/解锁时按 PP 决定禁用
    if(ppZero){ b.disabled=true; }
    b.onclick=()=>sendBattleCmd(2405,[sid]);
    sk.appendChild(b);
  });
}
async function ensureSkillNames(ids){
  const miss=[...new Set(ids.filter(sid=>_SKILL_NAMES[sid]==null))];
  if(!miss.length) return;
  try{
    const r=await fetch('/api/skills?ids='+miss.join(','));
    const j=await r.json();
    for(const [k,v] of Object.entries(j.skills||{})){
      _SKILL_NAMES[k]=(v&&v.name)||k;
      _SKILL_DATA[k]=v||{};
      const maxp=v&&v.pp;   // 最大 PP
      if(maxp!=null && _SKILL_PP[k]==null) _SKILL_PP[k]=maxp;   // 初始=最大pp
    }
    miss.forEach(sid=>{ if(_SKILL_NAMES[sid]==null) _SKILL_NAMES[sid]=String(sid); });
    refreshBattle();   // 拿到名字后重绘
  }catch(e){ miss.forEach(sid=>{ if(_SKILL_NAMES[sid]==null) _SKILL_NAMES[sid]=String(sid); }); }
}
document.getElementById('battleReportClearBtn').onclick=()=>{
  try{ fetch('/api/battle/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
  refreshBattle();
};
document.getElementById('battleReportCopyBtn').onclick=()=>{
  const rep=document.getElementById('battleReport');
  if(!rep) return;
  const lines=[...rep.childNodes].map(n=>n.textContent||'').filter(Boolean);
  const txt=lines.join('\n');
  try{ navigator.clipboard.writeText(txt); }catch(e){}
  const btn=document.getElementById('battleReportCopyBtn');
  const old=btn.textContent; btn.textContent='已复制'; setTimeout(()=>{ btn.textContent=old; }, 1200);
  appendLog({t:now(),level:'ok',msg:'已复制战报('+lines.length+'行)'});
};
async function openChangePetPicker(state, force){
  // 换宠: 在我方出战队伍(myTeam)里选一只, 发 2407 + 其 catchTime (int32).
  // 用**内联列表框**取代原生 prompt() (后者在嵌入式/部分环境会被拦截导致"点了没反应").
  // force=true: 强制换宠(当前精灵阵亡且无其它操作), 不提供"取消", 必须选一只。
  const picker=document.getElementById('battleChangePetPicker');
  const team=(state&&state.myTeam)||[];
  if(!picker) return;
  if(!team.length){ appendLog({t:now(),level:'warn',msg:'换宠: 我方出战队伍为空(需先收到2503), 无法选择'}); return; }
  const cur=state&&state.my && state.my.catchTime;
  const alive=team.filter(p=>p.catchTime!==cur && _petHp(p) > 0);
  const cands=(alive.length?alive:team.filter(p=>p.catchTime!==cur));
  if(!cands.length){ appendLog({t:now(),level:'warn',msg:'换宠: 没有可换的出战精灵'}); if(force){ 
      const st=document.getElementById('battleActionStatus'); if(st){ st.textContent='⚠️ 我方精灵阵亡, 但没有可上场的替补!'; st.style.color='var(--red)'; }
    } return; }
  const st=document.getElementById('battleActionStatus');
  // 渲染可点击的候选列表
  picker.style.display='block';
  picker.innerHTML='';
  const head=document.createElement('div');
  head.style.cssText='color:var(--accent);font:12px Menlo,monospace;margin-bottom:6px';
  head.textContent=force ? '⚠️ 我方精灵阵亡, 请选择场上精灵:' : '选择换上场的精灵:';
  picker.appendChild(head);
  for(const p of cands){
    const b=document.createElement('button'); b.type='button'; b.className='ops-btn';
    b.style.cssText=b.style.cssText+';flex:1 1 100%;text-align:left;display:block;margin-bottom:4px';
    b.textContent=((p.name||('id='+p.id))+'  Lv'+(p.level!=null?p.level:'?')+'  HP '+(_petHp(p)!=null?_petHp(p):'?')+'  catch='+p.catchTime);
    b.onclick=()=>{
      const catchTime=p.catchTime;
      picker.style.display='none'; picker.innerHTML='';
      doChangePet(catchTime, p, st);
    };
    picker.appendChild(b);
  }
  // 强制换宠时不提供"取消"; 普通换宠才显示
  if(!force){
    const cancel=document.createElement('button'); cancel.type='button';
    cancel.style.cssText='flex:1 1 100%;background:var(--elev);border:1px solid var(--line);border-radius:8px;color:var(--red);padding:6px;cursor:pointer;font:12px Menlo,monospace';
    cancel.textContent='取消';
    cancel.onclick=()=>{ picker.style.display='none'; picker.innerHTML=''; };
    picker.appendChild(cancel);
  }
  if(st){ st.textContent= force ? '⚠️ 强制换宠: 请选择上场精灵' : '请选择要换上场的精灵'; st.style.color='var(--amber)'; }
}
async function doChangePet(catchTime, chosen, st){
  try{ await fetch('/api/battle/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:('> 点击换宠 → '+(chosen&&(chosen.name||('id='+chosen.id)))+' (catch='+catchTime+')')})}); }catch(e){}
  if(st){ st.textContent='换宠请求 catch='+catchTime+'...'; st.style.color='var(--amber)'; }
  try{
    const r=await fetch('/api/battle/change-pet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catchTime:catchTime})});
    const j=await r.json();
    if(j.ok){
      if(st){ st.textContent='✔ 已发送 2407 换宠 catch='+catchTime; st.style.color='var(--green)'; }
      try{ await fetch('/api/battle/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:'  服务器已受理换宠 catch='+catchTime})}); }catch(e){}
      appendLog({t:now(),level:'ok',msg:'[对战] 已发送 2407 换宠 catch='+catchTime+' body='+(j.sent.body||'')});
    }else{
      if(st){ st.textContent='✘ 换宠失败: '+(j.error||''); st.style.color='var(--red)'; }
      appendLog({t:now(),level:'error',msg:'换宠失败: '+(j.error||'')});
    }
  }catch(e){
    if(st){ st.textContent='✘ 换宠出错: '+e; st.style.color='var(--red)'; }
    appendLog({t:now(),level:'error',msg:'换宠出错: '+e});
  }
}
async function sendBattleCmd(cmd, params){
  const st=document.getElementById('battleActionStatus'); if(st){ st.textContent='发送中 cmd='+cmd+'...'; st.style.color='var(--amber)'; }
  const label = (cmd===2405 && params && params.length) ? ('技能'+(_SKILL_NAMES[params[0]]||params[0])) : (window.__CM[cmd]||cmd);
  // 先把"点击"记进战报, 让战报立即有变化
  try{ await fetch('/api/battle/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:'> 点击发送 '+label+' (cmd='+cmd+')'})}); }catch(e){}
  try{
    const body=(params||[]).map(x=>String(x)).join(',');
    const r=await fetch('/api/battle/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:cmd, body:body})});
    const j=await r.json();
    if(j.ok){
      if(st){ st.textContent='✔ 已发送 cmd='+cmd+' '+label; st.style.color='var(--green)'; }
      try{ await fetch('/api/battle/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:'  服务器已受理发送 cmd='+cmd+' '+label})}); }catch(e){}
      appendLog({t:now(),level:'ok',msg:'[对战] 已发送 cmd='+cmd+' '+label+' body='+(j.sent.body||'')});
      // 技能 PP 由服务器 2505 的 AttackValue.skillList 权威同步 (renderBattleState 每次轮询会刷新);
      // 这里只是立即重绘一次, 让点击后的变化更直观(实际数值以服务器为准, 不本地扣减)
      if(cmd===2405 && params && params.length){
        const sid=params[0];
        const skc=document.getElementById('battleSkills');
        if(skc && (window.__lastBattleSkills||[]).length) renderSkillButtons(window.__lastBattleSkills);
      }
    }else{
      if(st){ st.textContent='✘ 发送失败: '+(j.error||''); st.style.color='var(--red)'; }
      appendLog({t:now(),level:'error',msg:'对战发包失败: '+(j.error||'')});
    }
  }catch(e){
    if(st){ st.textContent='✘ 发送出错: '+e; st.style.color='var(--red)'; }
    appendLog({t:now(),level:'error',msg:'对战发包出错: '+e});
  }
}
document.getElementById('battleHexBtn').onclick=async()=>{
  const hex=document.getElementById('battleHex').value.trim();
  if(!hex){ appendLog({t:now(),level:'tip',msg:'请先粘贴完整HEX包'}); return; }
  try{
    const r=await fetch('/api/battle/hex',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hex:hex})});
    const j=await r.json();
    if(j.ok) appendLog({t:now(),level:'ok',msg:'已发起对战包 cmd='+j.sent.cmd+' '+(window.__CM[j.sent.cmd]||'')});
    else appendLog({t:now(),level:'error',msg:'发起失败: '+(j.error||'')});
  }catch(e){ appendLog({t:now(),level:'error',msg:'发起出错: '+e}); }
};
document.getElementById('battleClearBtn').onclick=async()=>{
  try{ await fetch('/api/battle/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
  refreshBattle();
};
refreshBattle();
setInterval(refreshBattle, 800);   // 对战状态实时轮询

// 精灵按钮文本: 仅显示名字; 无名字则显示 id 数字 (统一背包+仓库)
function displayName(p){
  const n=(p && p.name || '').trim();
  if(n && !/^\(id=/.test(n)) return n;
  return String((p && p.id)!=null ? p.id : '?');
}
// 等级徽标: 黄字填充 + 黑色【外描边】 (黑色描边层在后, 黄色实心层在前), 黑体加粗
// -webkit-text-stroke 居中描边会吃掉黄字内部, 这里用双层叠加实现真·外描边
function makeLvBadge(level){
  const txt='LV.'+(level!=null?level:'?');
  const wrap=document.createElement('span');
  wrap.style.cssText='position:absolute;left:2px;bottom:2px;z-index:1;display:inline-block';
  const bk=document.createElement('span');   // 黑色外描边层 (在后)
  bk.textContent=txt;
  bk.style.cssText='position:absolute;left:0;top:0;color:#000;font:900 13px/1 "Heiti SC","SimHei","Microsoft YaHei",sans-serif;-webkit-text-stroke:1.5px #000;padding:0;';
  const fg=document.createElement('span');   // 黄色实心层 (在前)
  fg.textContent=txt;
  fg.style.cssText='position:relative;display:inline-block;color:#ff0;font:900 13px/1 "Heiti SC","SimHei","Microsoft YaHei",sans-serif;padding:0;';
  wrap.appendChild(bk); wrap.appendChild(fg);
  return wrap;
}
// ---- 背包 (43706 解析结果) ----
let bagData={first:[],second:[]};
let bagSel=null;   // {bag:'first'|'second', index}
let petSlotButtons=[];   // 每只精灵按钮的引用 {btn,pet,bag,index}, 供拖拽计算重叠
let emptySlotButtons=[]; // 背包空位按钮 {btn,bag,index}, 拖拽到空位 = 移至该背包
let suppressClick=false; // 拖拽结束后抑制一次 click
let dragState={active:false, copy:null, src:null};  // 拖拽状态

// 计算两个矩形在视口坐标下的重叠面积
function overlapArea(a, b){
  const w=Math.min(a.right,b.right)-Math.max(a.left,b.left);
  const h=Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top);
  return (w>0&&h>0)?w*h:0;
}
// 从 bag+index 推导 sortIndex (第一背包 1..6, 第二背包 7..12)
function sortIndexOf(slot){ return slot.bag==='second' ? (slot.index+7) : (slot.index+1); }

// 生成复制的精灵图标并进入拖拽 (复制图标也载入本地头像)
function startDrag(slot){
  dragState.active=true; dragState.src=slot;
  const copy=document.createElement('div');
  copy.style.cssText='position:fixed;z-index:1000;pointer-events:none;width:72px;padding:4px;background:var(--elev);border:1px solid var(--accent);border-radius:8px;text-align:center;color:var(--text);font:11px Menlo,monospace;box-shadow:0 4px 12px rgba(0,0,0,.5);font-size:10px';
  const ic=document.createElement('div'); ic.style.cssText='width:100%;aspect-ratio:1;background:var(--inset);border:1px solid var(--line);border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--muted);overflow:hidden';
  if(slot.pet.avatar){
    const im=document.createElement('img'); im.src=slot.pet.avatar; im.alt='';
    im.style.width='100%'; im.style.height='100%'; im.style.objectFit='contain';
    im.draggable=false; im.style.webkitUserDrag='none'; im.style.userSelect='none';
    ic.appendChild(im);
  }else{
    ic.textContent=(slot.pet.name||('id='+slot.pet.id)).slice(0,4);
  }
  copy.appendChild(ic);
  const cap=document.createElement('div'); cap.style.marginTop='4px'; cap.style.overflow='hidden'; cap.style.textOverflow='ellipsis'; cap.style.whiteSpace='nowrap'; cap.style.fontSize='10px';
  cap.textContent=(slot.pet.name||('id='+slot.pet.id))+' Lv'+(slot.pet.level!=null?slot.pet.level:'?');
  copy.appendChild(cap);
  dragState.copy=copy;
  document.body.appendChild(copy);
}
// 拖拽结束: 计算唯一最大重叠, 决定是否换位 (与首发重叠 -> 令该精灵首发)
function endDrag(){
  const copy=dragState.copy, slot=dragState.src;
  if(copy){
    const cr=copy.getBoundingClientRect();
    let best=null, bestEmpty=null, bestArea=0, tie=false;
    for(const o of petSlotButtons){
      if(o.btn===slot.btn) continue;
      const ar=overlapArea(cr, o.btn.getBoundingClientRect());
      if(ar>bestArea+0.5){ bestArea=ar; best=o; bestEmpty=null; tie=false; }
      else if(ar>0 && Math.abs(ar-bestArea)<0.5){ tie=true; }
    }
    for(const o of emptySlotButtons){
      const ar=overlapArea(cr, o.btn.getBoundingClientRect());
      if(ar>bestArea+0.5){ bestArea=ar; bestEmpty=o; best=null; tie=false; }
      else if(ar>0 && Math.abs(ar-bestArea)<0.5){ tie=true; }
    }
    if(best && bestArea>0 && !tie){
      const sk=slot.kind||'bag', tk=best.kind||'bag';
      if(sk==='storage' && tk==='bag'){ warehouseSwap(best, slot); }        // 仓库拖到背包: 互换
      else if(sk==='bag' && tk==='storage'){ warehouseSwap(slot, best); }  // 背包拖到仓库: 互换
      else if(sk==='bag' && tk==='bag'){
        const srcFirst = (slot.bag==='first' && slot.index===0);
        const tgtFirst = (best.bag==='first' && best.index===0);
        if(tgtFirst){ setDefaultPet(slot); }
        else if(srcFirst){ setDefaultPet(best); }
        else { swapPets(slot, best); }
      }
      // storage<->storage: 无操作
    } else if(bestEmpty && bestArea>0 && !tie){
      // 拖到另一背包空位 => 直接移动(非复制)
      const sk=slot.kind||'bag';
      if(sk==='storage'){ moveStorageToBag(slot.pet.catchTime, bestEmpty.bag); }        // 仓库 -> 该背包
      else if(sk==='bag' && slot.bag!==bestEmpty.bag){ moveBagPetToBag(slot, bestEmpty); } // 背包 -> 另一背包
    }
    if(copy.parentNode) copy.parentNode.removeChild(copy);
  }
  dragState.active=false; dragState.copy=null; dragState.src=null;
  suppressClick=true;
  setTimeout(()=>{ suppressClick=false; }, 400);
}
// 仓库精灵移至指定背包空位 -> /api/pets/move (2304 取仓库到背包)
async function moveStorageToBag(catchTime, bag){
  setTimeout(()=>{ refreshBag(); }, 2500);
  try{
    const r=await fetch('/api/pets/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'storage',catchTime:catchTime,bag:bag})});
    const j=await r.json();
    if(j.ok) appendLog({t:now(),level:'ok',msg:'仓库精灵 id='+catchTime+' 已移至'+(bag==='second'?'待命':'出战')+'背包'});
    else appendLog({t:now(),level:'error',msg:'移动失败: '+(j.error||'')});
  }catch(e){ appendLog({t:now(),level:'error',msg:'移动出错: '+e}); }
}
// 背包精灵移至另一背包空位 -> /api/pets/move (41462 换位, 目标(空)catchTime=0)
async function moveBagPetToBag(src, tgt){
  setTimeout(()=>{ refreshBag(); }, 2500);
  try{
    const r=await fetch('/api/pets/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'bag',catchTime:src.pet.catchTime,fromSort:sortIndexOf(src),toSort:sortIndexOf(tgt)})});
    const j=await r.json();
    if(j.ok) appendLog({t:now(),level:'ok',msg:'精灵 id='+src.pet.id+' 已移至'+(tgt.bag==='second'?'待命':'出战')+'背包'});
    else appendLog({t:now(),level:'error',msg:'移动失败: '+(j.error||'')});
  }catch(e){ appendLog({t:now(),level:'error',msg:'移动出错: '+e}); }
}
// 全局拖拽: mousedown 记录起点 -> 移动>阈值即生成复制图标; 松开时结算
let dragPending=null;   // {slot, x, y}
document.addEventListener('mousemove',(e)=>{
  if(dragPending && !dragState.active){
    if(Math.abs(e.clientX-dragPending.x)>6 || Math.abs(e.clientY-dragPending.y)>6){
      startDrag(dragPending.slot);
    }
  }
  if(dragState.active && dragState.copy){
    dragState.copy.style.left=(e.clientX-36)+'px';
    dragState.copy.style.top=(e.clientY-45)+'px';
  }
});
document.addEventListener('mouseup',()=>{
  if(dragState.active){ endDrag(); }
  dragPending=null;
});
// 触摸版拖拽: touchmove 移过阈值即产生复制图标并跟随; touchend 结算
document.addEventListener('touchmove',(e)=>{
  if(!(e.touches && e.touches[0])) return;
  const t=e.touches[0];
  if(dragPending && !dragState.active){
    if(Math.abs(t.clientX-dragPending.x)>6 || Math.abs(t.clientY-dragPending.y)>6){
      e.preventDefault(); startDrag(dragPending.slot);
    }
  }
  if(dragState.active && dragState.copy){
    e.preventDefault();
    dragState.copy.style.left=(t.clientX-36)+'px';
    dragState.copy.style.top=(t.clientY-45)+'px';
  }
},{passive:false});
document.addEventListener('touchend',(e)=>{
  if(dragState.active){ e.preventDefault(); endDrag(); }
  dragPending=null;
},{passive:false});
// 切换两只精灵位置 (41462)
async function swapPets(slotA, slotB){
  setTimeout(()=>{ refreshBag(); }, 2500);   // 操作后刷新背包(由后端再发43706, 这里兜底)
  try{
    const s1=sortIndexOf(slotA), c1=slotA.pet.catchTime;
    const s2=sortIndexOf(slotB), c2=slotB.pet.catchTime;
    const r=await fetch('/api/pets/swap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sortIndex1:s1,catchTime1:c1,sortIndex2:s2,catchTime2:c2})});
    const j=await r.json();
    if(j.ok) setStatus('ready');
    else appendLog({t:now(),level:'error',msg:'切换位置失败: '+(j.error||'')});
  }catch(e){ appendLog({t:now(),level:'error',msg:'切换位置出错: '+e}); }
}
// 设为首发 (PET_DEFAULT 2308 [catchTime])
async function setDefaultPet(slot){
  setTimeout(()=>{ refreshBag(); }, 2500);
  try{
    const r=await fetch('/api/pets/default',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catchTime:slot.pet.catchTime})});
    const j=await r.json();
    if(j.ok) appendLog({t:now(),level:'ok',msg:'已将 精灵 id='+slot.pet.id+' 设为首发'});
    else appendLog({t:now(),level:'error',msg:'设为首发失败: '+(j.error||'')});
  }catch(e){ appendLog({t:now(),level:'error',msg:'设为首发出错: '+e}); }
}
// 仓库精灵 <-> 背包精灵 互换
async function warehouseSwap(bagSlot, storageSlot){
  let ok=false;
  try{
    const r=await fetch('/api/pets/warehouse-swap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      bagCatchTime: bagSlot.pet.catchTime, bag: bagSlot.bag, storageCatchTime: storageSlot.pet.catchTime})});
    const j=await r.json();
    ok=!!j.ok;
    if(j.ok) appendLog({t:now(),level:'ok',msg:'仓库互换成功 (背包 id='+bagSlot.pet.id+' <-> 仓库 id='+storageSlot.pet.id+')'});
    else appendLog({t:now(),level:'error',msg:'仓库互换失败: '+(j.error||'')});
  }catch(e){ appendLog({t:now(),level:'error',msg:'仓库互换出错: '+e}); }
  if(!ok) return;
  // 先刷新背包(等 43706 处理并更新 bagData), 再刷新仓库(基于更新后的背包排除已在背包的精灵)
  await new Promise(res=>setTimeout(res, 1600));
  await refreshBag();
  await new Promise(res=>setTimeout(res, 600));
  await fetchStorage();
}

// ---- 精灵仓库 ----
let warehouseMode=false, warData=[], warSearch='', warType='normal', exeData=[], warSel=null;
// 星级养成信息展示
function renderCultInfo(pet){
  const el=document.getElementById('war-info'); if(!el) return;
  let h = '<div style="color:var(--muted);margin-bottom:4px">养成信息</div>'+
    '<div class="detail-grid">'+
    `<div><span class="k">属性</span><span class="v">${pet.attr?pet.attr:'?'}</span></div>`+
    `<div><span class="k">等级</span><span class="v">${pet.level!=null?pet.level:'?'}</span></div>`+
    `<div><span class="k">天赋</span><span class="v">${pet.dv!=null?pet.dv:'?'}</span></div>`+
    `<div><span class="k">性格</span><span class="v">${pet.nature!=null?pet.nature:'?'}</span></div>`+
    `</div>`;
  // 能力值 + 对应学习力 (排版与精灵详情一致: .abgrid 每排两项, 黄色学习力, 体力在最后)
  h += '<div class="detail-sec"><h3>能力值</h3><div class="abgrid">';
  const rows=[['攻击', pet.attack, pet.ev_attack], ['防御', pet.defence, pet.ev_defence],
              ['特攻', pet.s_a, pet.ev_sa], ['特防', pet.s_d, pet.ev_sd],
              ['速度', pet.speed, pet.ev_sp], ['体力', (pet.maxHp!=null?pet.maxHp:'?'), pet.ev_hp]];
  for(const [k,v,ev] of rows) h += `<div class="abcell"><span class="k">${k}</span><span class="v">${v!=null?v:'?'}</span><span class="ev">${ev!=null?ev:0}</span></div>`;
  h += '</div></div>';
  el.innerHTML = h;
}
// 仓库精灵养成信息: 用 2301 拉取完整 PetInfo (等级/天赋/性格)
async function showWarInfo(p){
  const el=document.getElementById('war-info'); if(!el) return;
  el.innerHTML='选中 精灵 id='+p.id+' (catchTime='+p.catchTime+') 拉取养成信息...';
  try{ await fetch('/api/pet-info/fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catchTime:p.catchTime})}); }catch(e){}
  for(let t=0;t<15;t++){
    await new Promise(r=>setTimeout(r,400));
    try{ const r=await fetch('/api/pet-info?catchTime='+p.catchTime); const j=await r.json();
      if(j.ok && j.pet){ renderCultInfo(j.pet); return; }
    }catch(e){}
  }
  renderCultInfo({level:p.level, dv:'?', nature:'?'});
}
// 背包选中精灵养成信息 (背包 PetInfo 已含 dv/nature)
function showBagInfo(){
  if(!bagSel) return;
  const p = bagSel.bag==='first' ? bagData.first[bagSel.index] : bagData.second[bagSel.index];
  if(p) renderCultInfo(p);
}
async function fetchExe(){
  exeData=[]; renderWarView();
  try{ await fetch('/api/exe/fetch',{method:'POST'}); }catch(e){}
  for(let t=0;t<20;t++){
    await new Promise(r=>setTimeout(r,400));
    try{ const r=await fetch('/api/exe'); const j=await r.json();
      if(j.ok && j.fetched){ exeData=j.pets||[]; break; }
    }catch(e){}
  }
  // 精英仓库本表不带等级(2361); 用与养成相同的 2301 渠道补齐缺失等级, 保证 LV 徽标正常显示.
  // 已缓存的(吃过养成)由后端跳过, 不重复发包.
  const miss=exeData.filter(p=>p.level==null);
  for(const p of miss){
    try{ await fetch('/api/pet-info/fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catchTime:p.catchTime})}); }catch(e){}
    await new Promise(r=>setTimeout(r,40));   // 稍作间隔, 避免一次灌太多 2301
  }
  // 等 2301 应答回填缓存后, 重新拉取已补齐等级的结果
  for(let t=0;t<30;t++){
    await new Promise(r=>setTimeout(r,400));
    try{ const r=await fetch('/api/exe'); const j=await r.json();
      if(j.ok){ exeData=j.pets||[]; if(exeData.every(q=>q.level!=null)) break; }
    }catch(e){}
  }
  renderWarView();
}
async function fetchStorage(){
  warData=[]; renderWarView();
  try{ await fetch('/api/storage/fetch',{method:'POST'}); }catch(e){}
  // 分页由后端发2503, 轮询 /api/storage 直到拿到数据
  for(let t=0;t<20;t++){
    await new Promise(r=>setTimeout(r,400));
    try{ const r=await fetch('/api/storage'); const j=await r.json();
      if(j.ok && j.fetched){ warData=j.pets||[]; break; }
    }catch(e){}
  }
  renderWarView();
}
function renderWarView(){
  const list=document.getElementById('war-list'); if(!list) return;
  list.innerHTML='';
  // 背包里已存在的精灵, 不计入仓库
  const bagCt=new Set();
  (bagData.first||[]).concat(bagData.second||[]).forEach(p=>{ if(p.catchTime) bagCt.add(p.catchTime); });
  // 数据源: 普通仓库(2303) 或 精英仓库(9015); 默认按 id 从大到小; 搜索按 id 过滤; 排除已在背包的
  const src = (warType==='exe' ? exeData : warData);
  let arr=src.slice().filter(p=>!(p.catchTime && bagCt.has(p.catchTime))).sort((a,b)=>(b.id||0)-(a.id||0));
  const q=warSearch.trim();
  if(q){ const qi=parseInt(q,10); if(Number.isFinite(qi)) arr=arr.filter(p=>p.id===qi); }
  if(!arr.length){ list.innerHTML='<div style="color:var(--muted)">（仓库为空或无匹配）</div>'; return; }
  arr.forEach(p=>{
    const b=document.createElement('button'); b.type='button'; b.className='av-btn'+(warSel===String(p.catchTime)?' sel':'');
    const img=document.createElement('div'); img.className='av-img'; img.style.position='relative';
    // 复制/展示都载入本地头像
    if(p.avatar){
      const im=document.createElement('img'); im.src=p.avatar; im.alt='';
      im.style.width='100%'; im.style.height='100%'; im.style.objectFit='contain';
      im.draggable=false; im.style.webkitUserDrag='none'; im.style.userSelect='none';
      im.onerror=()=>{ img.textContent=displayName(p); };
      img.appendChild(im);
    }else{
      img.textContent=displayName(p);
    }
    // 等级徽标: 头像图下层/左下角, 黄字黑外描边
    img.appendChild(makeLvBadge(p.level));
    const txt=document.createElement('div'); txt.className='av-txt';
    txt.textContent=displayName(p);
    b.appendChild(img); b.appendChild(txt);
    const slotRef={btn:b, pet:p, kind:'storage'};
    petSlotButtons.push(slotRef);
    b.onclick=(e)=>{ if(suppressClick){ suppressClick=false; return; } warSel=String(p.catchTime); renderWarView(); showWarInfo(p); };
    b.addEventListener('mousedown',(e)=>{ if(e.button!==0) return; dragPending={slot:slotRef, x:e.clientX, y:e.clientY}; });
    b.addEventListener('touchstart',(e)=>{ if(dragState.active) return; const t=e.touches&&e.touches[0]; if(!t) return; dragPending={slot:slotRef, x:t.clientX, y:t.clientY}; });
    list.appendChild(b);
  });
}
function updateWarehouseView(){
  const detail=document.getElementById('bag-detail-card');
  const war=document.getElementById('warehouse-view');
  if(warehouseMode){ detail.style.display='none'; war.style.display='block'; setWarTypeBtn(); if(warType==='exe') fetchExe(); else fetchStorage(); }
  else{ detail.style.display='block'; war.style.display='none'; }
}
async function refreshBag(){
  try{
    const r=await fetch('/api/bag'); const j=await r.json();
    if(!j.ok) return;
    bagData={first:j.first||[],second:j.second||[]};
    // 校验当前选中是否仍有效, 否则回退到首发(第一背包第1只)
    const cur = bagSel ? (bagSel.bag==='first'?bagData.first[bagSel.index]:bagData.second[bagSel.index]) : null;
    if(!cur){
      if(bagData.first.length) bagSel={bag:'first',index:0};
      else if(bagData.second.length) bagSel={bag:'second',index:0};
      else bagSel=null;
    }
    renderBag();
  }catch(e){}
}
function renderBag(){
  petSlotButtons=[];   // 重建时清空, 由背包+仓库重新填充
  emptySlotButtons=[]; // 背包空位(拖拽目标)
  renderAvRow('bag-first', bagData.first, 'first');
  renderAvRow('bag-second', bagData.second, 'second');
  renderDetail();
  if(warehouseMode) renderWarView();
}
function renderAvRow(containerId, arr, bag){
  const c=document.getElementById(containerId); if(!c) return; c.innerHTML='';
  // 每个背包固定 6 个位置 (3 个一排), 空的也占位显示
  const SLOTS=6;
  const m=Math.max(SLOTS, arr.length);
  for(let i=0;i<m;i++){
    const p=arr[i];
    if(p){
      const b=document.createElement('button'); b.type='button';
      b.className='av-btn'+(bagSel && bagSel.bag===bag && bagSel.index===i ? ' sel':'');
      const img=document.createElement('div'); img.className='av-img'; img.style.position='relative';
      if(p.avatar){                        // 有头像图
        const im=document.createElement('img'); im.src=p.avatar; im.alt='';
        im.style.width='100%'; im.style.height='100%'; im.style.objectFit='contain';
        im.draggable=false; im.style.webkitUserDrag='none'; im.style.userSelect='none';
        im.onerror=()=>{ img.textContent='头像'; };
        img.appendChild(im);
      }else{ img.textContent='头像'; }     // 无头像图, 占位
      // 等级徽标: 头像图下层/左下角, 黄字黑外描边
      img.appendChild(makeLvBadge(p.level));
      const txt=document.createElement('div'); txt.className='av-txt';
      txt.textContent=displayName(p);
      b.appendChild(img); b.appendChild(txt);
      const slotRef={btn:b, pet:p, bag:bag, index:i, kind:'bag'};
      petSlotButtons.push(slotRef);           // 每只精灵**只**一条记录 (此前重复 push 导致 endDrag 判定 tie, 拖拽失效)
      b.onclick=(e)=>{ if(suppressClick){ suppressClick=false; return; } bagSel={bag:bag,index:i}; renderBag(); if(warehouseMode){ warSel=null; showBagInfo(); } };
      // 拖拽: mousedown 记录起点, 一旦移动超过阈值即产生复制图标 (无需长按)
      b.addEventListener('mousedown',(e)=>{
        if(e.button!==0) return;
        dragPending={slot:slotRef, flag:false, x:e.clientX, y:e.clientY};
      });
      // 触摸版: touchstart 记录起点 (tap 仍由 click 选中, 移动即拖拽)
      b.addEventListener('touchstart',(e)=>{ 
        if(dragState.active) return;
        const t=e.touches && e.touches[0]; if(!t) return;
        dragPending={slot:slotRef, flag:false, x:t.clientX, y:t.clientY};
      });
      c.appendChild(b);
    }else{
      const e=document.createElement('div'); e.className='av-btn';
      const img=document.createElement('div'); img.className='av-empty'; img.textContent='';
      const txt=document.createElement('div'); txt.className='av-txt'; txt.textContent='空位';
      e.appendChild(img); e.appendChild(txt); c.appendChild(e);
      emptySlotButtons.push({btn:e, bag:bag, index:i});   // 记录空位, 供拖拽到空位=移至该背包
    }
  }
}
// ---- 技能详情 (缓存 + 弹窗) ----
const _skillCache={};
const skillModalEl=document.getElementById('skillModal');
async function fetchSkills(ids){
  const fresh=ids.filter(x=>x&&!_skillCache[x]);
  if(!fresh.length) return;
  try{
    const r=await fetch('/api/skills?ids='+encodeURIComponent(fresh.join(',')));
    const j=await r.json();
    if(j.ok && j.skills) Object.assign(_skillCache, j.skills);
  }catch(e){}
}
const _soulmarkCache={};   // 精灵物种id -> 魂印数据列表
async function fetchSoulmarks(ids){
  const fresh=ids.filter(x=>x&&!_soulmarkCache[x]);
  if(!fresh.length) return;
  try{
    const r=await fetch('/api/soulmarks?ids='+encodeURIComponent(fresh.join(',')));
    const j=await r.json();
    if(j.ok && j.soulmarks) Object.assign(_soulmarkCache, j.soulmarks);
  }catch(e){}
}
function escapeHtml(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function smText(t){ return String(t||'').replace(/\[sprite[^\]]*\]/gi,'').replace(/<[^>]+>/g,'').replace(/\r?\n/g,'\n').replace(/\|/g,'\n'); }
function renderSoulmarkPage(arr, page){
  if(!arr || !arr.length) return;
  if(page<0) page=0; if(page>arr.length-1) page=arr.length-1;
  const s=arr[page]||{};
  const desc=(s.desc||'').replace(/\[sprite[^\]]*\]/gi,'').replace(/<[^>]+>/g,'').replace(/\|/g,'\n');
  const tags=(s.tags&&s.tags.length)?s.tags.map(t=>`<span class="soulmark-tag">${t}</span>`).join(' '):'';
  const icon=s.iconId?`<div class="smark-modal-icon"><img src="/effecticon/${s.iconId}.png" onerror="this.remove()"></div>`:'';
  const nav=arr.length>1?`<div style="display:flex;justify-content:space-between;align-items:center;margin-top:12px">`+
    `<button class="smark-nav-btn" onclick="smPage(${page-1})" ${page<=0?'disabled':''}>上一版</button>`+
    `<span style="color:var(--muted)">${page+1} / ${arr.length}</span>`+
    `<button class="smark-nav-btn" onclick="smPage(${page+1})" ${page>=arr.length-1?'disabled':''}>下一版</button></div>`:'';
  document.getElementById('skillModalTitle').textContent='专属特性';
  document.getElementById('skillModalBody').innerHTML=
    icon+(tags?`<div style="text-align:center;margin-bottom:10px">${tags}</div>`:'')+
    `<div style="white-space:pre-line;line-height:1.7;color:var(--text);font-size:12px">${escapeHtml(desc)}</div>`+nav;
  skillModalEl.style.display='block';
}
let _curPetEffects=[];   // 最近渲染详情精灵的 effects (用于匹配当前专属特性阶段)
let _curSouls=[];        // 当前精灵实已开启的专属特性列表
function smPage(page){ renderSoulmarkPage(_curSouls, page); }
function openSoulmark(sid){
  const arr=_soulmarkCache[sid]||[];
  const ids=new Set((_curPetEffects||[]).map(e=>e.effectID));
  _curSouls=arr.filter(s=>ids.has(s.effectId));   // 只取与当前 effects 匹配的(已开启)阶段
  if(_curSouls.length) renderSoulmarkPage(_curSouls, 0);
}
function closeSkill(){ skillModalEl.style.display='none'; }
function row(k,v){ if(v==null||v==='') return ''; return `<div style="display:flex;justify-content:space-between;border-bottom:1px solid var(--elev);padding:4px 0"><span style="color:var(--muted)">${k}</span><span style="color:var(--text)">${v}</span></div>`; }
function openSkill(id){
  const d=_skillCache[id]; if(!d) return;
  const must=d.mustHit?'是':'否';
  const effs=(d.effects&&d.effects.length)?d.effects.map(e=>
    `<div style="margin-top:6px;padding:6px;background:var(--inset);border:1px solid var(--line);border-radius:6px">`+
      `<div style="color:var(--muted)">效果#<span style="color:var(--accent)">${e.id}</span>`+(e.tag?` · <span style="color:var(--accent)">${e.tag}</span>`:'')+`</div>`+
      (e.args&&e.args.length?`<div style="color:var(--muted)">参数: ${e.args.join(', ')}</div>`:'')+
      (e.desc?`<div style="color:var(--text);margin-top:2px">${e.desc}</div>`:'')+
    `</div>`).join('') : '<div style="color:var(--muted)">无附加效果</div>';
  document.getElementById('skillModalTitle').textContent='技能详情 - '+d.name;
  document.getElementById('skillModalBody').innerHTML=
    `<div style="font-size:14px;color:var(--text);margin-bottom:8px">${d.name} <span style="font-size:11px;color:var(--muted)">(技能id ${id})</span></div>`+
    row('属性', d.typeName)+row('威力', d.power)+row('PP值', d.pp)+
    row('命中率', (d.accuracy!=null?d.accuracy+'%':'?'))+
    row('暴击率', (d.crit?d.crit+'%':'无'))+
    row('是否必中', must)+row('先制等级', d.priority)+
    row('技能效果描述', '')+effs;
  skillModalEl.style.display='block';
}
skillModalEl.addEventListener('click',(e)=>{ if(e.target===skillModalEl) closeSkill(); });

async function renderDetail(){
  const p = bagSel ? (bagSel.bag==='first'?bagData.first[bagSel.index]:bagData.second[bagSel.index]) : null;
  const title=document.getElementById('bag-title');
  const det=document.getElementById('bag-detail');
  const storeBtn=document.getElementById('storeBtn');
  if(!p){ if(title)title.textContent='精灵详情'; if(det)det.innerHTML='<div style="color:var(--muted)">请选中一只精灵</div>'; if(storeBtn) storeBtn.disabled=true; return; }
  if(storeBtn) storeBtn.disabled=false;
  if(title) title.textContent='精灵详情 - '+(p.name||('id='+p.id));
  // 顶部: 名字+等级(仅当前等级)
  let h='<div style="font-size:14px;color:var(--text);margin-bottom:6px">'+ (p.name||('id='+p.id)) +'　<span style="color:var(--muted)">等级</span> <span style="color:var(--text)">'+(p.level!=null?p.level:'?')+'</span></div>';
  // 属性 + 养成: 天赋(去掉(dv)) + 性格
  h+='<div class="detail-grid">'+
     (p.attr?`<div><span class="k">属性</span><span class="v">${p.attr}</span></div>`:'') +
     (p.dv!=null?`<div><span class="k">天赋</span><span class="v">${p.dv}</span></div>`:'') +
     (p.nature!=null?`<div><span class="k">性格</span><span class="v">${p.nature}</span></div>`:'') +'</div>';
  // 能力值: 每排两项, 右侧黄色数值为学习力(不显示"学习力"三个字), 体力移至最后一项
  h+='<div class="detail-sec"><h3>能力值</h3><div class="abgrid">';
  const rows=[['攻击', p.attack, p.ev_attack], ['防御', p.defence, p.ev_defence],
              ['特攻', p.s_a, p.ev_sa], ['特防', p.s_d, p.ev_sd],
              ['速度', p.speed, p.ev_sp], ['体力', (p.maxHp!=null?p.maxHp:'?'), p.ev_hp]];
  for(const [k,v,ev] of rows) h+=`<div class="abcell"><span class="k">${k}</span><span class="v">${v!=null?v:'?'}</span><span class="ev">${ev!=null?ev:0}</span></div>`;
  h+='</div></div>';
  // 刻印: 三个并排图标 (后续补充图片)。暂置 SHOW_MARKS=false 隐藏, 改 true 恢复。
  const SHOW_MARKS=false;
  if(SHOW_MARKS){
    h+='<div class="detail-sec"><h3>刻印</h3><div class="marks-row">';
    for(const [lbl,val] of [['能力',p.abilityMark||0],['技能',p.skillMark||0],['通用',p.commonMark||0]])
      h+=`<div class="mark-icon"><span class="lbl">${lbl}刻印</span><span>${val||'无'}</span></div>`;
    h+='</div></div>';
  }
  // 专属特性 (魂印): 该精灵实际开启的阶段 = effects 中与物种某魂印 effectId 匹配者
  const smId=p.id;
  if(smId){ try{ await fetchSoulmarks([smId]); }catch(e){} }
  _curPetEffects=p.effects||[];
  const allSouls=smId?(_soulmarkCache[smId]||[]):[];
  const petEffIds=new Set((_curPetEffects||[]).map(e=>e.effectID));
  const sms=allSouls.filter(s=>petEffIds.has(s.effectId));   // 已开启的专属特性阶段
  const firstSm=sms[0]||{};
  const smIcon=firstSm.iconId?`<img class="smark-img" src="/effecticon/${firstSm.iconId}.png" onerror="this.style.display='none'">`:'';
  const smTags=(firstSm.tags&&firstSm.tags.length)?`<div class="smark-tags">${firstSm.tags.map(t=>`<span class="soulmark-tag">${t}</span>`).join('')}</div>`:'';
  h+='<div class="detail-sec"><h3>专属特性</h3><div class="smark-wrap">'+
     (sms.length?
       `<button class="smark-btn" onclick="openSoulmark(${smId})">${smIcon}<span class="smark-mid"><span class="smark-title">专属特性</span>${smTags}</span></button>`
       : '<div class="smark-btn-disabled">专属特性未解锁</div>')+'</div></div>';
  // 技能: 前4个 2x2 长方形图标, 第5个占两格宽在下方
  const skills=p.skills||[];
  const need=skills.map(x=>x&&x.id).filter(Boolean);
  if(need.length){ try{ await fetchSkills(need); }catch(e){} }
  h+='<div class="detail-sec"><h3>技能</h3><div class="skillgrid">';
  const skIcon=(s,extra='')=>{ const cls='sk'+extra;
    if(s && s.id){
      const d=_skillCache[s.id]||{};
      const nm=d.name?d.name:('技能'+s.id);
      return `<div class="${cls} sk-click" onclick="openSkill(${s.id})"><span class="sk-nm">${nm}</span>`+
        `<span class="sk-sub">${d.typeName?`<span class="sk-attr">${d.typeName}</span>`:''}${d.power?`<span class="sk-pow">威力${d.power}</span>`:''}</span>`+
        `<span class="pp">PP=${s.pp!=null?s.pp:'?'}</span></div>`;
    }else return `<div class="${cls}">空位</div>`; };
  for(let i=0;i<4;i++) h+=skIcon(skills[i]);
  h+=skIcon(skills[4],' sk5');   // 第5号位
  h+='</div></div>';
  det.innerHTML=h;
}
function g(k,v){ return `<div><span class="k">${k}</span><span class="v">${v}</span></div>`; }
setInterval(refreshBag, 2500);   // 轮询背包数据 (登录后自动填充)
refreshBag();

// ---- 切换阵容 ----
const teamModalEl=document.getElementById('teamModal');
const teamListEl=document.getElementById('teamList');
const teamNoteEl=document.getElementById('teamNote');
async function openTeams(){
  teamNoteEl.textContent='(正在拉取...)'; teamListEl.innerHTML='';
  teamModalEl.style.display='block';
  try{ await fetch('/api/teams/fetch',{method:'POST'}); }catch(e){}
  let got=null;
  for(let t=0;t<10;t++){
    await new Promise(r=>setTimeout(r,500));
    try{ const r=await fetch('/api/teams'); const j=await r.json();
      if(j.ok && j.fetched && j.teams.length){ got=j; break; }
    }catch(e){}
  }
  if(!got){ teamNoteEl.textContent='(未获取到阵容，请确认已登录且服务器支持)'; teamListEl.innerHTML=''; return; }
  teamNoteEl.textContent='(点选一套使用；当前使用 id='+got.curUsedId+')';
  teamListEl.innerHTML='';
  got.teams.forEach(team=>{
    const n=team.pet_detail.filter(x=>x[0]).length;
    const cur=team.id===got.curUsedId;
    const row=document.createElement('div');
    row.style.cssText='display:flex;align-items:center;gap:8px;padding:8px;margin:0 0 6px;background:'+(cur?'var(--accent-soft)':'var(--elev)')+';border:1px solid '+(cur?'var(--accent)':'var(--line)')+';border-radius:6px;cursor:pointer';
    row.innerHTML=`<div style="flex:1"><div style="color:${cur?'var(--accent)':'var(--text)'}">${team.nick||('阵容'+team.id)}${cur?' (使用中)':''}</div><div style="font-size:11px;color:var(--muted)">精灵 ${n} 只 · id=${team.id}</div></div>`;
    row.onclick=async()=>{
      if(cur){ return; }
      row.style.opacity='.5';
      let ok=false;
      try{
        const r=await fetch('/api/teams/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:team.id})});
        const j=await r.json();
        ok=!!j.ok;
        if(!ok){ teamNoteEl.textContent='切换失败: '+(j.error||''); row.style.opacity='1'; }
      }catch(e){ teamNoteEl.textContent='切换出错: '+e; row.style.opacity='1'; }
      if(ok){
        // 切换成功 => 关闭弹窗, 并同步刷新背包(43706)
        teamModalEl.style.display='none';
        setTimeout(()=>{ refreshBag(); }, 2500);
      }
    };
    teamListEl.appendChild(row);
  });
}
document.getElementById('teamBtn').onclick=()=>{ openTeams(); };
// 点击弹窗空白处(背板)关闭
teamModalEl.addEventListener('click',(e)=>{ if(e.target===teamModalEl) teamModalEl.style.display='none'; });
// 精灵仓库: 点击切换右半部分 仓库/详情
document.getElementById('wareBtn').onclick=()=>{ warehouseMode=!warehouseMode; updateWarehouseView(); };
// 仓库搜索 + 关闭搜索
document.getElementById('warSearch').addEventListener('input',(e)=>{
  warSearch=e.target.value;
  renderWarView();
  const c=document.getElementById('warClose');
  if(warSearch.trim()){ c.style.display='inline-block'; } else { c.style.display='none'; }
});
document.getElementById('warClose').onclick=()=>{
  warSearch=''; document.getElementById('warSearch').value='';
  renderWarView(); document.getElementById('warClose').style.display='none';
};
// 仓库类型切换: 普通仓库(2303) / 精英仓库(9015)
function setWarTypeBtn(){
  document.getElementById('warTypeNormal').className='warfilt'+(warType==='normal'?' active':'');
  document.getElementById('warTypeExe').className='warfilt'+(warType==='exe'?' active':'');
}
document.getElementById('warTypeNormal').onclick=()=>{ warType='normal'; setWarTypeBtn(); renderWarView(); };
document.getElementById('warTypeExe').onclick=()=>{ warType='exe'; setWarTypeBtn(); fetchExe(); };
// 入库: 把选中精灵放入仓库 (PET_RELEASE 2304 [catchTime, 0/3])
document.getElementById('storeBtn').onclick=async()=>{
  if(!bagSel) return;
  const p = bagSel.bag==='first' ? bagData.first[bagSel.index] : bagData.second[bagSel.index];
  if(!p || !p.catchTime){ appendLog({t:now(),level:'error',msg:'没有可入库的精灵'}); return; }
  try{
    const r=await fetch('/api/pets/store',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catchTime:p.catchTime, bag:bagSel.bag})});
    const j=await r.json();
    if(j.ok){ appendLog({t:now(),level:'ok',msg:'已入库 精灵 id='+p.id+' (catchTime='+p.catchTime+')'}); bagSel={bag:'first',index:0}; }
    else{ appendLog({t:now(),level:'error',msg:'入库失败: '+(j.error||'')}); }
  }catch(e){ appendLog({t:now(),level:'error',msg:'入库出错: '+e}); }
  setTimeout(()=>{ refreshBag(); }, 2500);
};
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="赛尔号协议调试 WebUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8680,
                    help="监听端口 (默认 8680; 若被占用可再换, 或留 0 自动选空闲端口)")
    ap.add_argument("--no-update", action="store_true",
                    help="启动时不检查/更新本地精灵头像 (默认会自动检查并按版本增量更新)")
    ap.add_argument("--update-force", action="store_true",
                    help="即使版本未变也强制重下载并解包精灵头像 (测试用)")
    args = ap.parse_args()

    # 启动前先确保本地"需要的文件"(目前=全部精灵头像)是最新的:
    # 记录并检查版本(assets_updater 用 data/head/.avatar_state.json 记录), 版本一致则跳过.
    # 需要解析时自动 pip 安装 UnityPy 到项目 vendor/, 再从 bundle 解出头像.
    if not args.no_update:
        try:
            from assets_updater import ensure_pet_avatars
            _upd = ensure_pet_avatars(force=args.update_force)
            if _upd.get("skipped"):
                log("info", f"头像已是最新 (版本 {_upd.get('version')}), 跳过更新")
            elif _upd.get("ok"):
                log("info", f"头像更新完成 (版本 {_upd.get('version')})")
            if _upd.get("error"):
                print(f"[头像更新] 警告: {_upd['error']}")
                log("warn", f"头像更新未完成: {_upd['error']} (继续使用现有头像)")
        except Exception as e:   # 任何异常都不阻断服务启动
            print(f"[头像更新] 未执行更新: {e}")
            log("warn", f"头像更新未执行: {e}")
    # 启动更新(或已是最新)后重读精灵数据, 让全新克隆首次部署即能看到属性/技能/魂印等
    _reload_data_maps()
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"端口 {args.port} 被占用: {e}")
        print("已自动改到空闲端口; 或换一个: python3 app/webui.py --port <端口>")
        srv = ThreadingHTTPServer((args.host, 0), Handler)
    actual = srv.server_address[1]
    print(f"赛尔号协议调试台: http://{args.host}:{actual}/")
    log("info", f"调试台已启动: http://{args.host}:{actual}/")
    log("tip", "填写米米号/密码后点『登录』; 登录过程与每个收发封包会实时输出到日志。")
    print("登录后可在页面发包; 日志实时流。Ctrl+C 退出。")

    # 把实际监听地址写入 webui_addr.json, 供 PySeer 脚本运行时自动定位后端
    write_addr_file(args.host, actual)

    def _on_sigterm(signum, frame):
        print("\n收到 SIGTERM, 正在保存日志...")
        save_logs("SIGTERM")
        srv.shutdown()

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出...")
    finally:
        save_logs("shutdown")


# ---- 可选扩展件: 若本地存在 app/multi.py, 其 install(globals(), Handler) 可为本后端注入额外能力.
# 缺省时后端行为不变.
if os.path.isfile(os.path.join(_SRC_DIR, "multi.py")):
    try:
        import multi
        multi.install(globals(), Handler)
    except Exception as _e:
        try:
            log("warn", f"扩展件加载失败(不影响原有行为): {_e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()

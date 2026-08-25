"""解析对战相关数据包 (依据 refs/fightinfo/ 反编译 AS, 大端 ByteArray/IDataInput).

主要覆盖:
    NOTE_READY_TO_FIGHT(2503) -> NoteReadyToFightInfo  (mode + 双方出战队伍)
    NOTE_START_FIGHT(2504)     -> FightStartInfo        (开场双方当前出战精灵画像)

以及与战斗中变化相关的子结构 (SiteBuffInfo / MarkBuffInfo / PetStatusEffectInfo 等).
所有字段按 IDataInput.readUnsignedInt (大端 u32) / readUnsignedShort(u16) / readUnsignedByte(u8)
/ readUTFBytes(n) 顺序读取, 与 refs/fightinfo 一致.
"""

from __future__ import annotations

from .petinfo import parse_effect as _pet_effect  # PetEffectInfo(24B)

# NoteReadyToFightInfo 里"特殊模式": mode 在这些值里时, 每只精灵读完整 PetInfo; 否则读浓缩战斗数组
SPECIAL_MODES = {14, 36, 37, 44, 45, 46, 47, 48, 49, 50, 51, 60, 66, 74, 75, 77,
                 78, 79, 80, 81, 82, 83, 88, 89, 100, 101, 102, 103, 104, 105, 106,
                 108, 109, 110, 112}


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def _i32(b, o):
    return int.from_bytes(b[o:o + 4], "big", signed=True)


def _u16(b, o):
    return int.from_bytes(b[o:o + 2], "big")


def _u8(b, o):
    return b[o]


def _name(b, o, n=16):
    """readUTFBytes(16) 名字字段 (utf8/gbk 尝试, 去尾部填充)."""
    raw = bytes(b[o:o + n]).rstrip(b"\x00\x20\xff")
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            s = raw.decode(enc)
            return "".join(ch for ch in s if ch != "\x00" and not (0x00 <= ord(ch) < 0x20))
        except Exception:
            continue
    return ""


# ---- 子结构 ----

def parse_site_buff(b, o):
    """SiteBuffInfo: siteBuffId(u16) + siteBuffTurn(u8)."""
    return {"siteBuffId": _u16(b, o), "siteBuffTurn": _u8(b, o + 2)}, o + 3


def parse_mark_buff(b, o):
    """MarkBuffInfo: cnt(u8) + {id(u16), markNum(u8)} × cnt."""
    cnt = _u8(b, o); o += 1
    arr = []
    for _ in range(cnt):
        arr.append({"id": _u16(b, o), "markNum": _u8(b, o + 2)}); o += 3
    return {"cnt": cnt, "arr": arr}, o


def parse_status_effect(b, o):
    """PetStatusEffectInfo: type(u32) + id(u32) + parm(u32)."""
    return {"type": _u32(b, o), "id": _u32(b, o + 4), "parm": _u32(b, o + 8)}, o + 12


def parse_fight_sign(b, o):
    """FightSignInfo: 读两个 u32; 第一个拆 id(16b)+lvNum(8b)+roundNum(8b), 第二个=spValue."""
    loc2 = _u32(b, o); o += 4
    sp = _u32(b, o); o += 4
    return {"id": loc2 & 0xFFFF,
            "lvNum": (loc2 >> 16) & 0xFF,
            "roundNum": (loc2 >> 24) & 0xFF,
            "spValue": sp}, o


# ---- FighterUserInfo (2503 每个用户) ----

def parse_fighter_user(b, o, mode, special):
    """一个 FighterUserInfo: 先读基段(id/nick/topLevel/support), 再读该用户出战精灵与装扮.

    返回 (dict, new_offset). pets 里的每只包含战斗所需字段 (浓缩数组) 或完整 PetInfo.
    """
    d = {}
    d["id"] = _u32(b, o); o += 4
    d["nick"] = _name(b, o, 16); o += 16
    d["topLevel"] = _u32(b, o); o += 4
    sup = _u32(b, o); o += 4
    d["support"] = []
    for _ in range(sup):
        d["support"].append({"pid": _u32(b, o), "skinId": _u32(b, o + 4)}); o += 8
    pet_num = _u32(b, o); o += 4
    d["petNum"] = pet_num
    d["pets"] = []
    for _p in range(pet_num):
        if special:
            # 特殊模式: 读完整 PetInfo (用 petinfo 解析)
            from .petinfo import parse_full
            pet, o = parse_full(b, o)
            pet["_full"] = True
        else:
            # 浓缩战斗数组
            catch = _u32(b, o); o += 4
            pid = _u32(b, o); o += 4
            hp = _i32(b, o); o += 4
            skill_cnt = _u32(b, o); o += 4
            skills = []
            for _s in range(skill_cnt):
                skills.append(_u32(b, o)); o += 4
            effect_cnt = _u16(b, o); o += 2
            effects = []
            for _e in range(effect_cnt):
                ef, o = _pet_effect(b, o)
                effects.append(ef)
            skin = _u32(b, o); o += 4
            pet = {"id": pid, "catchTime": catch, "hp": hp, "skills": skills,
                   "effects": effects, "skinId": skin}
        d["pets"].append(pet)
    cloth = _u32(b, o); o += 4
    d["clothNum"] = cloth
    d["cloth"] = []
    for _c in range(cloth):
        d["cloth"].append({"clothId": _u32(b, o), "v": _u32(b, o + 4)}); o += 8
    o += 4   # 尾部一个 u32
    return d, o


def parse_note_ready_to_fight(body):
    """2503 NOTE_READY_TO_FIGHT 包体 -> NoteReadyToFightInfo.

    返回 {mode, efFightType, isSpecial, my, other}. my/other 为 FighterUserInfo(含 pets).
    """
    b = bytes(body)
    o = 0
    mode = _u32(b, o); o += 4
    ef = _u32(b, o); o += 4
    special = mode in SPECIAL_MODES
    u0, o = parse_fighter_user(b, o, mode, special)
    u1, o = parse_fighter_user(b, o, mode, special)
    # 2503 的 two users: 先读的用户为 我方/敌方 由 id 与当前账号匹配决定, 这里按序保留, 由调用方比对
    return {"mode": mode, "efFightType": ef, "isSpecial": special,
            "userA": u0, "userB": u1, "end_offset": o}


# ---- FightPetInfo (2504) ----

def parse_fight_pet_info(b, o):
    """FightPetInfo (开场/回合中某方当前出战精灵画像).

    依据 refs/fightinfo/FightPetInfo.as 完整解析 (含 FightSignInfo 签名段与 lockedSkillArr).
    """
    d = {}
    d["userID"] = _u32(b, o); o += 4
    d["petID"] = _u32(b, o); o += 4
    d["petName"] = _name(b, o, 16); o += 16
    d["catchTime"] = _u32(b, o); o += 4
    d["hp"] = _i32(b, o); o += 4
    d["maxHP"] = _u32(b, o); o += 4
    d["lv"] = _u32(b, o); o += 4
    d["catchType"] = _u32(b, o); o += 4
    from .petinfo import parse_resistance as _res
    d["resistance"], o = _res(b, o)      # 56B
    d["skinId"] = _u32(b, o); o += 4
    chg = _u32(b, o); o += 4
    d["changehps"] = []
    for _ in range(chg):
        d["changehps"].append({
            "id": _u32(b, o), "hp": _u32(b, o + 4), "maxhp": _u32(b, o + 8),
            "lock": _u32(b, o + 12), "chujueNumber": _u32(b, o + 16),
            "chujueRound": _u32(b, o + 20)}); o += 24
        _mb, o = parse_mark_buff(b, o)
    d["requireSwitchCthTime"] = _u32(b, o); o += 4
    d["xinHp"] = _u32(b, o); o += 4
    d["xinMaxHp"] = _u32(b, o); o += 4
    d["isChangeFace"] = _u32(b, o); o += 4
    d["secretLaw"] = _u32(b, o); o += 4
    run_cnt = _u32(b, o); o += 4
    d["skillRunawayMarks"] = []
    for _ in range(run_cnt):
        d["skillRunawayMarks"].append(_u32(b, o)); o += 4
    d["holyAndEvilThoughts"] = _u32(b, o); o += 4
    d["yearVip2022_shengjian"] = _u32(b, o); o += 4
    d["yearVip2022_chujue"] = _u32(b, o); o += 4
    d["siteBuff"], o = parse_site_buff(b, o)
    d["bothSiteBuff"], o = parse_site_buff(b, o)
    d["markBuff"], o = parse_mark_buff(b, o)
    sign_cnt = _u32(b, o); o += 4
    d["signInfo"] = []
    for _ in range(sign_cnt):
        sig, o = parse_fight_sign(b, o)
        d["signInfo"].append(sig)
    d["lockedSkillArr"] = []
    for _ in range(5):
        d["lockedSkillArr"].append(_u32(b, o)); o += 4
    d["_incomplete"] = False
    return d, o


def parse_fight_start_info(body):
    """2504 NOTE_START_FIGHT 包体 -> FightStartInfo (双方当前出战精灵 FightPetInfo).

    依据 FightStartInfo.as: [isCanAuto u32][isShowFightHp u32] + 2 × FightPetInfo.
    FightPetInfo 顺序不定 (按 userID==actorID 决定 my/other), 这里返回两个原始 FightPetInfo.
    """
    b = bytes(body)
    o = 0
    is_can_auto = _u32(b, o); o += 4
    is_show_hp = _u32(b, o); o += 4
    a, oa = parse_fight_pet_info(b, o)
    c, oc = parse_fight_pet_info(b, oa)
    return {"isCanAuto": is_can_auto, "isShowFightHp": is_show_hp,
            "fightPetA": a, "fightPetB": c, "end_offset": oc}


def parse_change_pet_info(b, o=0):
    """ChangePetInfo (战斗中换宠的一只精灵) — 依据 refs/fightinfo/ChangePetInfo.as."""
    d = {}
    d["userID"] = _u32(b, o); o += 4
    d["petID"] = _u32(b, o); o += 4
    d["catchTime"] = _u32(b, o); o += 4
    d["petName"] = _name(b, o, 16); o += 16
    d["level"] = _u32(b, o); o += 4
    d["hp"] = _u32(b, o); o += 4
    d["maxHp"] = _u32(b, o); o += 4
    sk = _u32(b, o); o += 4
    d["skillList"] = []
    for _ in range(sk):
        d["skillList"].append([_u32(b, o), _u32(b, o + 4)]); o += 8
    from .petinfo import parse_resistance as _res
    d["resistance"], o = _res(b, o)
    d["skinId"] = _u32(b, o); o += 4
    chg = _u32(b, o); o += 4
    d["changehps"] = []
    for _ in range(chg):
        d["changehps"].append({
            "id": _u32(b, o), "hp": _u32(b, o + 4), "maxhp": _u32(b, o + 8),
            "lock": _u32(b, o + 12), "chujueNumber": _u32(b, o + 16),
            "chujueRound": _u32(b, o + 20)}); o += 24
        _mb, o = parse_mark_buff(b, o)
    d["xinHp"] = _u32(b, o); o += 4
    d["xinMaxHp"] = _u32(b, o); o += 4
    d["isChangeFace"] = _u32(b, o); o += 4
    run_cnt = _u32(b, o); o += 4
    d["skillRunawayMarks"] = []
    for _ in range(run_cnt):
        d["skillRunawayMarks"].append(_u32(b, o)); o += 4
    for k in ("holyAndEvilThoughts", "yearVip2022_shengjian", "yearVip2022_chujue",
              "laborDay2022_yinji", "suli2022", "mulian2022"):
        d[k] = _u32(b, o); o += 4
    d["siteBuff"], o = parse_site_buff(b, o)
    d["bothSiteBuff"], o = parse_site_buff(b, o)
    d["markBuff"], o = parse_mark_buff(b, o)
    sign_cnt = _u32(b, o); o += 4
    d["signInfo"] = []
    for _ in range(sign_cnt):
        sig, o = parse_fight_sign(b, o)
        d["signInfo"].append(sig)
    d["lockedSkillArr"] = []
    for _ in range(5):
        d["lockedSkillArr"].append(_u32(b, o)); o += 4
    d["commonChangeFaceValue"] = _u32(b, o); o += 4
    return d, o


def parse_use_pet_item(b, o=0):
    """UsePetItemInfo (战斗内用道具) — 依据 refs/fightinfo/UsePetItemInfo.as."""
    d = {}
    d["userID"] = _u32(b, o); o += 4
    d["itemID"] = _u32(b, o); o += 4
    d["userHP"] = _u32(b, o); o += 4
    d["changeHp"] = _i32(b, o); o += 4
    d["round"] = _u32(b, o); o += 4
    return d, o


def parse_catch_pet(b, o=0):
    """CatchPetInfo (捕捉野宠) — 依据 refs/fightinfo/CatchPetInfo.as."""
    return {"catchTime": _u32(b, o), "petID": _u32(b, o + 4)}, o + 8


def parse_note_use_skill(body):
    """2505 NOTE_USE_SKILL 回合结果 (依 refs/captured 实战包确认).

    前导 16 字节(首个技能记录): [userID u32][skillID u32][count u32][actorCatchTime u32].
    我方技能在包体最前面; 敌方技能则**内嵌在包体更深处的同级子块**(byte 级偏移,
    不按 4 对齐), 可用 extract_skill_use_records() 完整找出双方本回合使用的技能.

    尾部包含受击/受影响精灵的 [catchTime u32][hp u32][maxhp u32] 记录 (hpUpdates),
    据此可做**逐回合血量刷新**. 中间的伤害/经验/属性等字段布局依赖 FightManager 类(未收录),
    此处不解.
    """
    b = bytes(body)
    head = {"userID": _u32(b, 0) if len(b) >= 4 else None,
            "skillID": _u32(b, 4) if len(b) >= 8 else None,
            "count": _u32(b, 8) if len(b) >= 12 else 0,
            "actorCatchTime": _u32(b, 12) if len(b) >= 16 else None}
    head["hpUpdates"] = extract_pet_hp_updates(b)
    head["skillRecords"] = extract_skill_use_records(b)
    return head


def extract_skill_use_records(b):
    """从 2505 回合包体里找出**所有**技能使用记录.

    实测 (谱尼 Boss 战, mode=67) 里, 同一回合包体会出现两条同构的 16 字节记录,
    每条 = [userID u32][skillID u32][count u32][actorCatchTime u32]:
        我方:  userID == 出战账号, 位于包体前导 (offset 0)
        敌方:  userID == 0, 内嵌于包体深处 (byte 级偏移, 需逐字节扫描)
    这两条 count 相同(即本回合数), 说明一个 2505 = 一整回合, 含双方各自动作.

    由于 2505 前导 / 中间可能有 1~3 字节的变长字段 (buff 计数等), 敌方子块**不按 4 对齐**,
    所以这里逐字节滑动匹配, 并做守卫避免误判. 返回按出现顺序的列表:
        [{userID, skillID, count, actorCatchTime}, ...]
    """
    segs = []
    seen = set()
    n = len(b)
    for o in range(0, n - 15):
        uid = _u32(b, o)
        sk = _u32(b, o + 4)
        cnt = _u32(b, o + 8)
        c = _u32(b, o + 12)
        # 必须是 catchTime 区间 + 技能ID区间 + 合理回合数 + 合法用户身份
        if not (0x40000000 <= c <= 0x7FFFFFFF):
            continue
        if not (10000 <= sk <= 65000):
            continue
        if not (1 <= cnt <= 40):
            continue
        if uid != 0 and uid < 1000:
            continue
        key = (uid, sk, cnt, c)
        if key in seen:
            continue
        seen.add(key)
        segs.append({"userID": uid, "skillID": sk, "count": cnt, "actorCatchTime": c})
    return segs


def extract_pet_hp_updates(b):
    """扫描 2505/回合包里的精灵血量记录, 兼容两种布局.

    格式A(相邻三段式): [catchTime u32][hp u32][maxhp u32]        (refs/captured 6v1 用)
    格式B(32B记录):   [catchTime u32][type u32][1 u32][val u32][0][0][hp u32][maxhp u32]
                      (1v1/普通对战实测: hp/accmaxhp 在 +24/+28)

    catchTime∈[0x40000000..0x7FFFFFFF], 0 < hp <= maxhp < 10_000_000.
    返回 [{catchTime, hp, maxHp}, ...] (按出现顺序, 去重).
    """
    out = []
    seen = set()
    n = len(b)

    def add(ct, hp, mh):
        # 允许 hp==0(精灵阵亡), 但 maxHp 必须 >1 且合理 (排除误读的 [ct][0][1])
        if 0x40000000 <= ct <= 0x7FFFFFFF and 0 <= hp <= mh and 1 < mh < 10_000_000 and ct not in seen:
            seen.add(ct)
            out.append({"catchTime": ct, "hp": hp, "maxHp": mh})

    for o in range(0, n - 7, 1):
        ct = _u32(b, o)
        if not (0x40000000 <= ct <= 0x7FFFFFFF):
            continue
        # 格式A: hp/maxhp 紧邻
        add(ct, _u32(b, o + 4), _u32(b, o + 8))
        # 格式B: [ct][type][1][val][0][0][hp][maxhp]
        if _u32(b, o + 8) == 1 and _u32(b, o + 16) == 0 and _u32(b, o + 20) == 0:
            add(ct, _u32(b, o + 24), _u32(b, o + 28))
    return out


# 2506 结果包体(FightOverInfo)里我方账号的字节偏移与尾部结果标志
_FIGHT_OVER_ACCT_OFF = 5       # 实测: 我方账号 id (u32, 大端) 位于 body offset 5..8
_FIGHT_OVER_TAIL_OFF = -1      # 实测: 尾部 1 字节 = 本场回合数 (0x02/0x03), 非胜负标志


def parse_fight_over(body):
    """2506 FIGHT_OVER / 对战结束结果包 (FightOverInfo).

    实测包体 57B, 几乎全为 0, 有效字段:
        - offset 5 (u32, 大端): 我方账号 id (与 2503/2504 的 me_id 一致)
        - 末字节: 本场回合数 (用于旁证"打了几回合"; 不是胜负标志)

    注意: **该包不含胜负标志**。胜负需结合本场最后一次 2505 的 HP 更新判断:
    (敌方 HP==0 → 我方胜; 我方 HP==0 且敌方>0 → 我方败) —— 由调用方结合已跟踪的对战状态给出.
    返回 {accountId, roundNum, endOffset}.
    """
    b = bytes(body)
    n = len(b)
    acc = _u32(b, _FIGHT_OVER_ACCT_OFF) if n >= 9 else None
    if acc is not None and (acc == 0 or acc > 0x7FFFFFFF):
        acc = None
    tail = b[_FIGHT_OVER_TAIL_OFF] if b else None
    return {"accountId": acc, "roundNum": tail, "endOffset": n}

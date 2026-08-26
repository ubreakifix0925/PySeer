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
    """ChangePetInfo (战斗中换宠的一只精灵) — 依据 refs/fightinfo/ChangePetInfo.as.

    任一子字段越界时**优雅退出**(返回已解到的最远偏移), 兼容不同长度包体.
    """
    d = {}
    try:
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
        d["_incomplete"] = False
    except (IndexError, ValueError):
        d["_incomplete"] = True
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
    """2505 NOTE_USE_SKILL 回合结果 (依据 refs/attack/UseSkillInfo.as + AttackValue.as 精确解).

    一个 2505 = 一整回合 = `UseSkillInfo`, 内含 `firstAttackInfo` + `secondAttackInfo`
    两个 AttackValue, pack 首尾相接 (我方块在最前, 敌方块紧随). 读取顺序见
    parse_attack_value(), 对每个实战包**逐字节验证可精确消耗到末尾**.

    first(我方) 与 second(敌方, userID==0) 各自给全字段, 含:
        skillID, atkTimes, lostHP(对敌伤害), realHurtHp, gainHP, remainHP(结算后当前HP),
        maxHp, isCrit, state, petStatus, status/specailArr/sideEffects/changehps 等.

    附加: extract_pet_hp_updates() (按 catchTime 扫描的 hp/maxhp 记录) 覆盖**背包全体**
    精灵(含被波及的换下宠物), 而 attack 块只含两个施法者, 两者互补.
    """
    b = bytes(body)
    if len(b) < 44:
        return {"error": "short", "attackBlocks": [], "hpUpdates": [], "skillRecords": []}
    try:
        usi = parse_use_skill_info(b)
    except (ValueError, IndexError):
        # 占位/哨兵包 (如 0xDEADBEEF guard) 或非有效 UseSkillInfo
        return {"error": "guard", "attackBlocks": [], "hpUpdates": [], "skillRecords": []}
    head = {
        "userID": (usi.get("first") or {}).get("userID"),
        "skillID": (usi.get("first") or {}).get("skillID"),
        "count": (usi.get("first") or {}).get("atkTimes"),
        "actorCatchTime": _u32(b, 12) if len(b) >= 16 else None,
    }
    head["hpUpdates"] = extract_pet_hp_updates(b)
    head["skillRecords"] = extract_skill_use_records(b)
    head["attackBlocks"] = extract_attack_blocks(b)
    head["first"] = usi.get("first")
    head["second"] = usi.get("second")
    head["endOffset"] = usi.get("endOffset")
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


def parse_attack_value(b, o):
    """按 refs/attack/AttackValue.as 逐字段解一个 AttackValue, 返回 (dict, new_offset).

    该类的读取顺序已**逐字节验证**: 对每个实战 2505, 连续解两个 AttackValue 恰好
    消耗到包体末尾 (0 误差). 字段如下 (大端, 除注明外为 u32):

        userID, skillID,
        (±2 个丢弃 u32: 回合/开局信息)
        effectName(u32=FightEffectName.id), atkTimes, lostHP, realHurtHp,
        gainHP(i32), remainHP(i32)   <- 施法者**结算后当前 HP**
        maxHp, state, petStatus,
        [skillList: count + (skillID, pp)×count],
        isCrit,
        status(uint8 count + 逐字节),
        specailArr(u32 count + u32×count; 含 changeValue@[13], changeValue2@[25], changeSelfValue@[37], changeEnemyValue@[38]),
        sideEffects(u32 count + PetStatusEffectInfo(12B)×count),
        battle_lv(i32), change_bitset, priority,
        immunizationStates(u32 count + u32×count),
        changehps(u32 count + {id,hp,maxhp,lock,chujueNumber,chujueRound}×count + MarkBuffInfo each),
        requireSwitchCthTime, maxHpSelf, maxHpOther, secretLaw,
        skillRunawayMarks(u32 count + u32×count),
        siteBuff(u16+u8), bothSiteBuff(u16+u8), markBuff(u8 cnt + (u16,u8)×cnt),
        signInfo(u32 count + FightSignInfo(8B)×count),
        lockedSkillArr(5×u32),
        skillResult(u32 count + u32×count),
        zhuijiId, zhuijiHurt.
    """
    def u(n=4):
        nonlocal o
        v = int.from_bytes(b[o:o + n], "big"); o += n; return v
    def i(n=4):
        nonlocal o
        v = int.from_bytes(b[o:o + n], "big", signed=True); o += n; return v

    x = {}
    x["userID"] = u()
    x["skillID"] = u()
    u(); u()                      # 2 个丢弃 u32 (回合/开局)
    x["effectName"] = u()
    x["atkTimes"] = u()
    x["lostHP"] = u()
    x["realHurtHp"] = u()
    x["gainHP"] = i()
    x["remainHP"] = i()           # 结算后当前 HP
    x["maxHp"] = u()
    x["state"] = u()
    x["petStatus"] = u()
    sln = u()
    x["skillList"] = [[u(), u()] for _ in range(sln)]
    x["isCrit"] = u()
    sc = u(1)
    x["status"] = [u(1) for _ in range(sc)]
    w = u()
    x["specailArr"] = [u() for _ in range(w)]
    se = u()
    x["sideEffects"] = []
    for _ in range(se):
        x["sideEffects"].append({"type": u(), "id": u(), "parm": u()})
    x["battle_lv"] = i()
    x["change_bitset"] = u()
    x["priority"] = u()
    im = u()
    x["immunizationStates"] = [u() for _ in range(im)]
    ch = u()
    x["changehps"] = []
    for _ in range(ch):
        c = {"id": u(), "hp": u(), "maxhp": u(), "lock": u(),
             "chujueNumber": u(), "chujueRound": u()}
        mb = u(1)
        x["changehps"].append({**c, "_markBuff": [{"id": u(2), "markNum": u(1)} for _ in range(mb)]})
    x["requireSwitchCthTime"] = u()
    x["maxHpSelf"] = u()
    x["maxHpOther"] = u()
    x["secretLaw"] = u()
    sw = u()
    x["skillRunawayMarks"] = [u() for _ in range(sw)]
    x["siteBuff"] = {"id": u(2), "turn": u(1)}
    x["bothSiteBuff"] = {"id": u(2), "turn": u(1)}
    mb = u(1)
    x["markBuff"] = [{"id": u(2), "markNum": u(1)} for _ in range(mb)]
    sn = u()
    x["signInfo"] = []
    for _ in range(sn):
        loc = u(); sp = u()
        x["signInfo"].append({"id": loc & 0xFFFF, "lvNum": (loc >> 16) & 0xFF,
                              "roundNum": (loc >> 24) & 0xFF, "spValue": sp})
    x["lockedSkillArr"] = [u() for _ in range(5)]
    sr = u()
    x["skillResult"] = [u() for _ in range(sr)]
    x["zhuijiId"] = u()
    x["zhuijiHurt"] = u()
    return x, o


def parse_use_skill_info(body):
    """2505 NOTE_USE_SKILL = UseSkillInfo = [firstAttackInfo][secondAttackInfo].

    只有**本回合实际出手**的两个施法者才各有 AttackValue; 若某方本回合未出手
    (如敌方已阵亡/未动), 则对应 AttackValue 不存在或退化. 一般:
        firstAttackInfo.userID == 我方账号, secondAttackInfo.userID == 0(敌方).
    返回 {first, second, endOffset}. 若包体过短/为哨兵(0xDEADBEEF)或凑不齐第二个,
    则 second 为 None 或整包判定为"非有效 2505".

    守卫: 真实 UseSkillInfo 至少要有两个 AttackValue 可用; 若 first 的 userID/长度异常
    (如 deadbeef guard 或 len<啥都不是), 直接抛 ValueError 让调用方跳过.
    """
    b = bytes(body)
    if len(b) < 44:
        raise ValueError("2505 包体过短, 非有效 UseSkillInfo")
    first, o = parse_attack_value(b, 0)
    # 哨兵 guard: userID==0xDEADBEEF 或 first 全是 0 且包体极短, 视为无效
    if first.get("userID") == 0xDEADBEEF or (first.get("userID") == 0 and len(b) < 100):
        raise ValueError("2505 为占位/哨兵包, 跳过")
    second = None
    if o < len(b):
        try:
            second, o = parse_attack_value(b, o)
        except Exception:
            second = None
    return {"first": first, "second": second, "endOffset": o}


def extract_attack_blocks(b):
    """便捷: 返回 [firstAttackValue, secondAttackValue] 的简要字段列表.

    每个元素含 userID/skillID/atkTimes/lostHP(伤害)/gainHP(回血)/realHurtHp/remainHP/
    maxHp/isCrit/state/petStatus/status/specailArr/sideEffects/skillList/changehps.
    跳过**空块**(skillID==0 且 userID==0 且 remainHP==0 且 maxHp==0): 表示该方本回合
    未出手(敌已阵亡/未动/回合未进行), 不当作有效攻击者.
    """
    r = None
    try:
        r = parse_use_skill_info(b)
    except (ValueError, IndexError):
        return []
    out = []
    for blk in (r.get("first"), r.get("second")):
        if blk is None:
            continue
        # 空块判定: 施法者未实际出招 (技能0/无伤害/无血量/无状态)
        if (blk.get("skillID") == 0 and blk.get("userID") == 0
                and blk.get("remainHP") == 0 and blk.get("maxHp") == 0
                and blk.get("lostHP") == 0 and blk.get("gainHP") == 0):
            continue
        out.append({
            "userID": blk.get("userID"),
            "skillID": blk.get("skillID"),
            "atkTimes": blk.get("atkTimes"),
            "lostHP": blk.get("lostHP"),
            "gainHP": blk.get("gainHP"),
            "realHurtHp": blk.get("realHurtHp"),
            "remainHP": blk.get("remainHP"),
            "maxHp": blk.get("maxHp"),
            "isCrit": blk.get("isCrit"),
            "state": blk.get("state"),
            "petStatus": blk.get("petStatus"),
            "status": blk.get("status"),
            "specailArr": blk.get("specailArr"),
            "sideEffects": blk.get("sideEffects"),
            "skillList": blk.get("skillList"),
            "changehps": blk.get("changehps"),
        })
    return out


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
        # 允许 hp==0(精灵阵亡), 但 maxHp 必须合理 (排除误读的 [ct][0][1] 与 tiny 垃圾记录;
        # 真实对战精灵 maxHp 至少 ~几十, 而误读常在 1..5). 上限防溢出/噪声.
        if 0x40000000 <= ct <= 0x7FFFFFFF and 0 <= hp <= mh and 10 < mh < 10_000_000 and ct not in seen:
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


# 2506 结果包体 (FightOverInfo) 逐字段解码 — 依 refs/attack/FightOverInfo.as
def parse_fight_over(body):
    """2506 FIGHT_OVER / 对战结束结果包 (FightOverInfo).

    按 refs/attack/FightOverInfo.as 解码 (实测包体 57B 恰好消费完):
        type(u8), reason(u32), winnerID(u32), isCanSave(u32),
        twoTimes/threeTimes/autoFightTimes/btlDetectTimes/energyTimes/learnTimes (各 u32),
        deltaTopLv(i32), deltaTopHonour(u32), maxH(u32), totalH(u32), roundNum(u32).

    **winnerID** 即胜者账号 —— 我方账号 => 我方胜; 敌方(0) => 我方负. reason 说明结束原因.
    roundNum 为本场回合数. 返回 {type, reason, winnerID, isCanSave, maxH, totalH,
    roundNum, deltaTopLv, deltaTopHonour, endOffset}.
    """
    b = bytes(body)
    o = 0

    def u(n=4):
        nonlocal o
        v = int.from_bytes(b[o:o + n], "big"); o += n; return v
    def i(n=4):
        nonlocal o
        v = int.from_bytes(b[o:o + n], "big", signed=True); o += n; return v

    d = {"type": u(1), "reason": u(), "winnerID": u(), "isCanSave": u() != 0}
    d["twoTimes"] = u(); d["threeTimes"] = u(); d["autoFightTimes"] = u()
    d["btlDetectTimes"] = u(); d["energyTimes"] = u(); d["learnTimes"] = u()
    d["deltaTopLv"] = i(); d["deltaTopHonour"] = u()
    d["maxH"] = u(); d["totalH"] = u(); d["roundNum"] = u()
    d["endOffset"] = o
    return d

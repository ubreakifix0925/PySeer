# 赛尔号（Seer）脚本与数据工具 — 开发成果整理

> 本文档汇总基于 **PySeer 后端**（`assets_updater.py` 自更新数据管线 + `webui.py` 协议调试台）完成的一系列成果：自更新游戏数据、精灵详情界面功能、协议逆向结论，以及供脚本使用的第三方库 `PySeer.py`。
> 基础登录/WebUI 使用说明见 [README.md](../README.md)。

---

## 1. 总览

![架构]（概念）

```
游戏资源(ConfigPackage bundle / DefaultPackage bundle)
        │  assets_updater.py (启动时自动下载+解析, 可自更新)
        ▼
petbook.json / pet_attr.json / skills.json / soulmarks.json / data/effecticon/*.png
        │  由 webui.py 启动加载, 供界面回填
        ▼
WebUI(http://127.0.0.1:8680)  ── 后台已登录的 SeerClient  ──▶ PySeer.py (脚本库)
        │                                                   send/recv/get_value
        └── 精灵详情(属性/能力值/专属特性/技能) + 拖拽换位 + 弹窗
```

核心目标：**所有游戏数据都来自游戏资源本身、可随版本自更新**（不依赖静态他人解析表）。

---

## 2. 自更新数据管线（`assets_updater.py`）

启动时 `ensure_pet_avatars()` 会保证 UnityPy 可用，并从 ConfigPackage 包下载/解析多份数据（各自有版本状态文件，命中版本即跳过）。

| 产物 | 数据源（ConfigPackage bundle 内） | 解析函数（Solaris 移植） | 内容 |
|---|---|---|---|
| `petbook.json` | `petbook.bytes` | `extract_petbook_names` | 精灵 id→名字（~4901 条） |
| `pet_attr.json` | `monsters.bytes` | `parse_monsters` | 基表精灵(id, real_id==0) → 属性中文名（5112 条） |
| 属性名来源 | `skilltypes.bytes` | `parse_skill_types` | 属性 id→中文名（138 种） |
| `skills.json` | `moves.bytes` + `skill_effect.bytes` | `parse_moves` / `parse_skill_effects` / `regenerate_skills` | 技能 id → 名/PP/属性/威力/命中/暴击/必中/先制/效果（27206 条） |
| `soulmarks.json` | `effecticon.bytes` + `effectag.bytes` | `parse_effect_icons` / `parse_effect_tags` / `regenerate_soulmarks` | 精灵 id → 专属特性魂印列表（1993 只） |
| `data/effecticon/*.png` | DefaultPackage `effecticon_1..5.bundle` | `ensure_effect_icons` | 魂印/效果图标（2114 张，按 icon_id 命名） |
| `item_names.json` | `itemsoptimizecatitems{N}.bytes` + `midleitems.bytes` + `midleexchangeitems.bytes` | `assets_updater.regenerate_item_names` | 物品 id → 物品名（32055 条，时装/收藏/主道具/宠物道具/中间物品/资源等） |

> 物品名提取（已并入 `assets_updater.py` 的自更新管线，输出 `data/item_names.json` + `.item_names_state.json`）：
> 物品定义为 C# 风格二进制表，多数类用"固定 K"对齐（物品 id 恰在名字长度前缀前 K 字节，
> K∈{8,12,16}，实测 ~100% 命中）；宠物道具/药剂（`itemsoptimizecatitems3`）记录内为可变长字段，
> 布局 `[id][u32][u32][u16 前缀][前缀][i32][u16 名][名]...`，改用逐字节定位带内(300xxx)id 并向前解出；
> `midleitems`/`midleexchangeitems`（中间/交换物品，含合成材料与"购买/一键…"类杂项物品）同为"id+名字"二元表；
> 资源/货币类（`itemsoptimizecatitems0`，赛尔豆/钻石/燃料…）id 为 1..15 的小序号（独立资源 id 系统）。
> 也可独立刷新：`PYTHONPATH=vendor/unitypy python3 analysis/extract_item_names.py`（读缓存 bundle，不联网）。

每份数据都由**同一 ConfigPackage bundle**（`cache/petbook/<fh>.bundle`，含 `petbook.bytes`/`monsters.bytes`/`skilltypes.bytes`/`moves.bytes`/`skill_effect.bytes`/`effecticon.bytes`/`effectag.bytes`）解析，随游戏版本自动刷新；状态文件：`.petbook_state.json` / `.pet_attr_state.json` / `.skills_state.json` / `.soulmarks_state.json`（均已加入 `.gitignore`）。

> ⚠️ **版本获取的关键：CDN 缓存穿透**。官方 CDN 对「内容会变但 URL 不变」的
> `PackageManifest_<Pkg>.version` 会返回**陈旧缓存**（不加任何参数拿到旧版，导致新精灵解不出）。
> `assets_updater.py` 的 `_http_get()` **默认给每个 CDN 请求追加时间戳查询参数
> `?_cb=<ms>`**，强制 CDN 回源，从而拿到**线上游戏实际版本**（这正是不依赖任何游戏本地文件、
> 纯后端即可拿到正确资源的关键）。版本号是 `YYYYMMDDHHMMSS` 的**任意时间戳**，非整点，因此
> 靠打网格探测不可靠；权威来源即缓存穿透后的 `.version`。

### 关键解析结构（来自真实 Solaris 解析器）

**monsters.bytes**——每条记录各段均为可选，前面各带 1 字节布尔开关：
```
atk char combo def 名字(u16+utf8) evolv(3)
[flag] extra_moves?  free_forbidden gender hp id
[flag] learnable_moves?  [flag] move?  pet_class real_id
[flag] show_extra_moves?  sp_atk sp_def  [flag] sp_extra_moves?
spd support transform type vip is_fly is_ride
```
`learnable_moves` 内部又是 `[flag]adv_move? [flag]move? [flag]sp_move?`，每条 16B（move）/20B（adv,sp，带 `tag2`）。**属性在 `type` 字段**；只取 `real_id==0` 的基表记录（其 `id` 即物种编号）。

**skilltypes.bytes**——属性类型：id→中文名（1–20 单属性；21–132 双属性；**221–226 王/混沌/神灵/轮回/虫/虚空**）。条目 = `att(cn组合id串) cn(中文名) en(可选英文名列表) id is_dou`。

**moves.bytes**——技能：名/pp/type/power/accuracy/crit/mustHit/priority + `side_effect`(效果id列表) + `side_effect_arg`(扁平参数，每个效果按 `argsNum` 消耗)；效果描述模板在 `skill_effect.bytes`（`{0}/{1}/{2}` 占位，用 `format(*args)` 填成可读文本）。

**effecticon.bytes + effectag.bytes**——专属特性（魂印）：`pet_id`（拥有者列表）/`tips`(描述)/`analyze`/`effect_id`/`args`/`kind`(标签id，**0 基，对应 `effectag` 的 tag id 需 +1**，对齐 Solaris `tag_map[tag_id+1]`)/`icon_id`。

---

## 3. 协议逆向结论

**PetInfo（2301/43706 应答）结构**（依据 `refs/PetInfo.as`, `refs/PetSkillInfo.as`, `refs/PetEffectInfo.as`, `refs/PetResistanceInfo.as` 反编译，大端 ByteArray）：
```
前段标量(能力值等)
  → 5×PetSkillInfo (8B: id+pp)
  → 8×u32(捕获/刻印: catchTime,catchMap,catchRect,catchLevel,abilityMark,skillMark,commonMark,commonMarkActived)
  → effectCount(u16) + N×PetEffectInfo (24B/个: itemId|status|leftCount|effectID|8×[a,checkAdd])
  → PetResistanceInfo (56B)
  → skinId, assistMoveId → 3×u32(能力值6×16位) → 6×{base,pvp,pve}_total → 3×curHp
```
- 解析器见 `seer/petinfo.py`（`parse_front`/`parse_skill`/`parse_effect`/`parse_resistance`/`parse_full`/`split_petbag_43706`）。
- **批量分割**：`parse_full` 返回下一只起始偏移，据此按 `[第一背包数][pet1]..[第二背包数][petN]` 切开 43706 全部精灵（已验证）。

**专属特性（魂印）阶段 —— 关键发现**（用真实 43706 包，两只同 id=3266 精灵：一只开启、一只未开启）：
- 开启者 `effects` 含一条 `PetEffectInfo`（`effectID=880`），未开启者 `effectCount=0`。
- `effectID=880` 恰为该物种(3266) 魂印的 `effect_id`（soulmark#594）。
- ⇒ **精灵持有专属特性的阶段 = 其 `effects` 中与该物种某魂印 `effectId` 匹配的那条**；无匹配则未解锁；多魂印物种则命中的那条即当前阶段。

**会话密钥离线破解（gamedump4，`=e10e6f7cd2`）**：
- 密钥派生规则（参考 52pojie `t1468888`）：收到 `LOGIN_IN(1001)` 响应后，取**明文最后 4 字节**为“密钥种子”，与**米米号异或**，该值转字符串，取其 **md5 前 10 字符**即会话密钥（`seer/client.py::derive_session_key`、`mock_server.py::derive_seed_key` 一致）。
- 本抓包缺 1001 响应（种子），故用**已知明文攻击**：登录后 C2S 包明文头固定为 `[ver=0x31][cmd][uid=米米号][res]`，对每个候选 `seed` 算 `key=md5(str(seed^账号))[:10]`，解一个 C2S 包，若 `ver==0x31 && uid==账号` 则为候选，再用**全部 C2S 包复验**（真密钥应 ~100% 命中，伪命中极低）。
- 账号 = **<账号>**（非 <另一号>；后者只是 8080 心跳通道的会话/令牌号）。
- 结果：`seed=532711005` → 会话密钥 **`e10e6f7cd2`**，解码 744 个 C2S 包全部为真实命令（GET_PET_INFO 458、GET_SIM_USERINFO 53、USER_FOREVER_VALUE 36、PET_RELEASE 24、GET_MULTI_FOREVER 19、SWITCH_MAP…），命令名与 `cmdmap.json` 吻合。
- 脚本：`crack_seed_v3.py`（弱 oracle 收集候选 → 全部 C2S 复验）。注意：单用 `ver+uid` 会因密钥其余字节自由而产生“对齐巧合”伪命中（如 `eaeeff7cd2` 只命中 645/754 且 cmd 乱码），必须用全部 C2S 复验排除。
- 结论与遗留：**C2S 方向已完全破解**；S2C（服务器→客户端）帧在该抓包中并非干净的 `[4B总长][cipher]`（即使按 seq 拼接再切帧，用该密钥也解不出 `ver=0x31`），疑似大帧跨 TCP 段/不同帧结构，方向性密钥差异，留作后续。

**封包头 ver 字节 = 包类型/方向标记（本轮对战流抓包确认，并非版本号）**：
- `0x31` = 客户端→服务器（C2S，请求/指令）。
- `0x01` = 服务器→客户端**主动推送**（NOTE），如 `NOTE_READY_TO_FIGHT`/`NOTE_START_FIGHT`/`PET_BOOK_UPDATE`/`LOAD_PERCENT`。
- `0x3E` = 服务器→客户端，**对客户端某条请求的直接应答**，如 `MIBAO_FIGHT`/`NEW_TEAM_REFRESH_INFO`/`READY_TO_FIGHT`/`USE_SKILL`/`GET_PET_INFO`。
- 实现：`seer/client.py::_ver_kind()` 据此返回 `request/note/response/unknown`；`recv_game_packet()` 返回值带 `kind` 字段；`recv_until()` 用 `kind != 'note'` 判定"真正应答"，避免把服务器 NOTE 推送误当成交答。

---

## 4. WebUI 功能（`webui.py`，http://127.0.0.1:8680）

### 精灵详情
- **属性**（`pet_attr.json`）：显示 属性/天赋/性格/能力值（含学习力）。
- **专属特性**（核心）：设计为 `sk5` 尺寸按钮——左侧专属特性图标（`/effecticon/<iconId>.png`），中间“专属特性”四字 + 下方标签徽标（免伤/免疫异常/先制…），点击弹出面板：描述（`|`→换行）+ 多版本翻页（一页一个阶段）。未开启则显示“专属特性未解锁”。
  - **显示的是该精灵实际开启的阶段**：前端用 `p.effects` 的 `effectID` 集合匹配 `soulmarks.json[species]` 中 `effectId` 相近者。
- **技能**：技能格显示 技能名/属性/威力/PP（此前仅 `技能id+pp`）；点击弹出技能详情弹窗（属性/威力/PP/命中/暴击/必中/先制 + 各效果的 id/参数/描述）。
- **刻印**：已暂时隐藏（`const SHOW_MARKS=false`，改 `true` 即恢复显示）。

### 拖拽复制交互补充
拖拽（复制图标）结束时同时检测**精灵槽位**与**空位**；命中**另一背包的空位**时改为**直接移动**：
| 源 | 拖到空位位置 | 行为 |
|---|---|---|
| 出战背包(第一) | 待命背包(第二)空位 | 移至待命背包 |
| 待命背包(第二) | 出战背包(第一)空位 | 移至出战背包 |
| 仓库 | 出战背包空位 | 移至出战背包 |
| 仓库 | 待命背包空位 | 移至待命背包 |

后端 `/api/pets/move`：`kind='storage'`→发 **2304**(PET_RELEASE) 取仓库到背包（位置码 1→第一/2→第二）；`kind='bag'`→发 **41462**(换位) `[fromSort,catchTime,toSort,0]`（目标空位 catchTime=0）。

### 脚本页（第三个页签“脚本”）
- **左侧**：展示默认脚本目录（`webui.py` 同目录下的 `scripts/`，`SCRIPTS_DIR`）里的所有 `.py` 脚本，点击选中后点“运行选中脚本”即可后台启动（`subprocess` 子进程，`PYTHONPATH` 已含项目根目录，可 `import PySeer`）；运行中可点“停止脚本”。
- **脚本输出控制台**（新增）：右半区顶部「脚本输出 (实时)」一栏，脚本运行时其 stdout/stderr（print 等）**实时**逐行显示在这里（前端单独渲染 `level='script'` 的日志，不混进封包日志）；每次运行前自动清空，自带“清空”按钮。
- **右侧**：自上而下为「脚本输出 (实时)」+「② 日志输出」(实时)；「③ 发包测试」则移到**左半脚本列表下方**。登录页仅保留“① 登录操作”。
- **自动跳转**：登录成功后（状态 `ready`）自动切换到“脚本”页。
- 后端接口：`GET /api/scripts`（列目录+是否运行中）、`POST /api/scripts/run`（{name}，含路径穿越校验）、`POST /api/scripts/stop`。

### 对战页（第四个页签“对战”）
- **图形化对战**：轮询 `/api/battle`，图形化显示**我方/敌方**当前出战精灵的头像（`data/head/<id>.png`）、血条(`hp/maxHP`)、等级、以及各自**出场队伍**（2503）的精灵缩略图。
- **技能/操作按钮**：下方一排技能按钮（来自我方当前出战精灵的 `skills`），点击即发 **2405 USE_SKILL**（包体=技能id）；另有 换宠(2407)/用药(2406)/逃跑(2410)/捕捉(2409) 操作按钮。**换宠**会弹出我方出战队伍选择器，选好后发 `2407 + 目标catchTime`（int32），不用再手填空包体。
- **实时更新**：后台 `on_frame` 解析 **2503 NOTE_READY_TO_FIGHT**（`NoteReadyToFightInfo`，双方队伍+血量+技能）与 **2504 NOTE_START_FIGHT**（`FightStartInfo`，开场当前出战精灵画像）填充 `_BATTLE`，前端每 800ms 轮询刷新。
- **自动切换（检测到对战即打开）**：后台 `on_frame` 监听到**对战开始（2503 出场队伍）**且此前不在对战中时，推送一条 **`level='battle'`** 的日志事件；前端 SSE 收到后立即 `refreshBattle()`，若 `/api/battle` 显示 `active && !finished` 就**自动切到“对战”页签**并开始监听展示整场对战流程。前端另每 800ms 轮询 `/api/battle`（若对战已在进行——如非本页发起的对战——也会自动切过去）。**“对战”标签上会亮起绿色圆点**表示对战进行中；对战结束(2506)或清空后复位，下一场对战可再次自动切换。这样脚本/后台一旦打起对战，界面会自动弹到对战页并实时显示；已进行的对战不会强制拉回你当前页签（只对“新一场对战开始”自动切）。
- **发起对战**：输入**带 cmdid 的完整 HEX 包**（支持任意对战命令号，如 41129 之外的命令），`/api/battle/hex` 从包头提取命令号+包体，按当前账号重建 uid/序列号并加密封包发送。
- 解析器：`seer/fightinfo.py`（`parse_note_ready_to_fight`/`parse_fight_start_info`/`parse_change_pet_info`/`parse_fight_sign` 等）。**2504 `FightPetInfo` 已完整解析**（含 `FightSignInfo`=8B：`id(16b)|lvNum(8b)|roundNum(8b)` + `spValue`(u32)，以及 `lockedSkillArr`），因此 2504 能解出**双方**当前出战精灵。
- **逐回合刷新**：后台 `on_frame` 处理——**2505 NOTE_USE_SKILL**（精确解出双方 `AttackValue`，见「双方技能识别」）会**按 catchTime 逐回合刷新**双方当前精灵与双方队伍的 HP（`extract_pet_hp_updates` 兼容相邻三段式/32B 两种布局 / `_apply_hp_updates`）；**2407 CHANGE_PET** 按 `ChangePetInfo`（新入场精灵完整状态）更新对应一方当前精灵与其在队伍的记录，并刷新可用技能栏；2406 用道具、2409 捕捉会解析并写日志。
- **换宠闭环**：客户端**发** `2407 CHANGE_PET`，包体 = 目标精灵 catchTime（int32 大端，`/api/battle/change-pet`）；**收** 2407 应答即 `ChangePetInfo`（新上场精灵），`parse_change_pet_info` 依 `refs/fightinfo/ChangePetInfo.as` 逐字段解（已验证精确消费到末尾）。`userID==0`→敌方NPC换宠（存 `NpcChangePetData` 延迟应用）；其余→存 `changePetData` 在 `onUseSkill` 回合内应用。**强制换宠**：当**我方当前精灵阵亡**（`my.hp<=0`，兼容 `xinHp`）且**我方队伍里仍有存活替补**时，对战页自动弹出换宠选择器（`_isForcedChange`），并**禁用技能与除换宠外的操作按钮**（`_setBattleOpsLocked`，技能按其 `data-ppzero` 额外禁用），直到选一只替换（2407）后强制态解除、按钮恢复；换宠选择器统一读 `_petHp(p)`（`hp` 或 `xinHp` 回退）判定存活。
- **双方技能识别（敌我，已依 AttackValue.as 完整解出）**：一个 **2505 = 一整回合 = `UseSkillInfo`**，内含 `firstAttackInfo` + `secondAttackInfo` 两个 **AttackValue**，首尾相接（我方块最前、敌方块随后）。`parse_attack_value()` 按 `refs/attack/AttackValue.as` 逐字段解（并对每个实战包**逐字节验证精确消耗到末尾**）：`userID, skillID, (2×丢弃), effectName, atkTimes, lostHP(伤害), realHurtHp, gainHP(回血), remainHP(结算后HP), maxHp, state, petStatus, [skillList], isCrit, status(u8数组), specailArr, sideEffects(PetStatusEffectInfo列表), battle_lv/change_bitset/priority, immunizationStates, changehps, requireSwitchCthTime, maxHpSelf, maxHpOther, secretLaw, skillRunawayMarks, siteBuff/bothSiteBuff/markBuff, signInfo, lockedSkillArr(5), skillResult, zhuijiId, zhuijiHurt`。**`remainHP/maxHp` = 双方施法精灵结算后权威血量**（斩杀回合敌方 remainHP==0），`lostHP`=本技能造成伤害，`isCrit`=暴击。敌块 `userID==0`，斩妖回合敌块为空块（全0，`extract_attack_blocks()` 已跳过）。实测敌方(`uid=0`)技能：**10995 旋灭裂空阵(圣灵·135，伤害40-343)**、**20500 圣光气(普通·0·必中，回复/属性类，伤害0)**；我方 `37381 星光·浪打千击(水·30·必中，每回合暴击，伤害1269-3301)`。`specailArr` 语义索引：`[5..9]`状态、`[10]`追击败、`[11]`lostHP、`[14]`reSetAliveNum、`[26]`变身标志。
- **战报记录（精简）**：`_BATTLE.report` 按时间记录整场对战，只保留三类信息——`对战开始(mode/双方数量)`、`开场双方当前精灵`、**每回合约一条** `回合 N [我方/敌方] 名字(id) 使用技能 技能名[技能id] 剩余HP h/m [暴击] [⚠️阵亡]`（由 2505 的 `AttackValue.remainHP/maxHp` 权威血量驱动，`换宠` 也会记录一条）→`对战结束 —— 结果: 胜/负`；不再打印逐精灵的掉血/回血明细、道具/捕捉细节、结束包原始字段等。`/api/battle` 返回 `report`，对战页底部「战报记录」整栏**从上到下(时间正序)** 实时展示（仅当用户停在底部时才跟随滚动；可清空/复制）。**点击技能等操作会先通过 `/api/battle/action` 记入 `> 点击发送…`**。**每场结束(2506)仍会把本场所有完整(解密后)包体写入 `webui_logs/battle_<时间>.log`** 供研究，历史包体另持续追加到 `webui_logs/battle_capture.log`——这两份日志**不**随战报精简，仍保留完整细节。
- **对战结束结果**：**2506 FIGHT_OVER**（`FightOverInfo`）经 `parse_fight_over()` 按 `refs/attack/FightOverInfo.as` 解（包体57B 恰好消费完）：`type(u8), reason(u32), **winnerID(u32)**, isCanSave(u32), twoTimes/threeTimes/autoFightTimes/btlDetectTimes/energyTimes/learnTimes(各u32), deltaTopLv(i32), deltaTopHonour(u32), maxH(u32), totalH(u32), roundNum(u32)`。**`winnerID` = 胜者账号**（我方账号→我方胜利；0→我败/未开打）——权威判定。实测战胜场 `winner==<账号>, reason=0`；取消场 `winner=0, reason=1`。战报输出 `对战结束 —— 结果: …` + 最终双方血量 + 结束包(winID/原因/回合数)。
- 注：`refs/BattleFightManager.as` 是 **BattleRoyale（雪球大战/大逃杀）** 管理器（`BATTLEROYALE_*`/`RoyaleUserInfo`/道具伤害），`refs/FightManager.as` 是**发起对战**（fightWithPlayer/Npc/Boss、MIBAO_FIGHT、邀请）；两者均**不解析**精灵对战 2505 回合包，2505 的完整结构仍需 **`com.robot.app.fight.FightManager`**，其派发的**回合/结算类在 `com.robot.core.info.fightInfo.attack.*`**（如 `attack.FightOverInfo`，见 `FightManager.as` import）。**PetFightDLL** 是 `FightManager._petFightClass` 指向的**动态战斗引擎类**：`FightManager.setup()` 里写死 `_petFightClass = "PetFightDLL_201308"`（另有 `"PetFightDLL"` 一版，靠缓冲记录 939 切换，但当前固定 201308）；它由 `getDefinitionByName` 按**完全限定类名**实例化，`petFightClass` getter 只返回**短名**，其完整包路径为 `com.robot.app.fight.PetFightDLL_201308` / `com.robot.app.fight.PetFightDLL`（与 `com.robot.app.fight.FightManager` 同包）。**真正逐字段解 2505 回合结果的读者类就在这个 PetFightDLL 里**，本仓库未收录其源码。

---

## 5. 脚本库 `PySeer.py`（供赛尔号脚本用）

> 📘 本部分的**完整 API / 用法 / 示例**另见专项文档 [docs/PySeer.md](./PySeer.md)。

后端启动并登录后，脚本用本库即可让后端发/收包并取值（自包含，仅 stdlib `urllib`）。

```python
from PySeer import Seer
s = Seer()          # 运行时自动指向已登录后端 (无需在代码里硬编码地址)
s.send(43706)                             # ① 发送函数: 发 SEND 包(id + 参数列表)
pkt = s.recv(2301, [3266, 0, 0, 0])       # ② 接收函数: 发 SEND + 等 RECV, 返回完整包体(Packet)
v = s.get_value(pkt, 0)                   # ③ 取值函数: 取包体第 0 个 int32
print(pkt.ints, pkt.body, v)
```

**后端地址自动发现**：`Seer()` 不传参即按 `discover_backend()` 自动定位，优先级为
`显式参数 > 环境变量 SEER_BACKEND > 后端启动时写入的 webui_addr.json > 逐端口探测附近仍在线的后端 > 兜底 http://127.0.0.1:8680`。
后端 `webui.py` 启动时会把**实际监听地址**（含 `--port 0` 自动选的端口）写入 `webui_addr.json`，
因此脚本无需也不应硬编码 `Seer("http://127.0.0.1:8680")`。**当 `webui_addr.json` 指向的后端已下线时**，
会自动逐端口探测（默认 `8680..8699`，用 `/api/status` 判定存活）并回退到仍在线的实例。

| 函数 | 说明 | 参数 | 返回 |
|---|---|---|---|
| `send(cmd, params)` | 发送 SEND 包（不等待响应） | `cmd`(id 或命令名如 `ENTER_MAP`)、`params`(参数列表) | 后端应答 dict |
| `recv(cmd, params, timeout=8)` | 发送并**等待该命令的 RECV** | 同上 + 超时 | `Packet`(`body`=完整包体hex, `ints`=十进制, `raw`=bytes) |
| `get_value(body, index)` | 从包体取第 `index` 个值（int32 大端） | `body`(Packet/hex/bytes)、`index` | int |
| `get_recv_value(cmd, params, index, timeout=8)` | **一步"发包→等 RECV→取应答第 index 个值"**：等价 `get_value(recv(cmd,params), index)` | `cmd`、`params`(发送包体)、`index`(应答参数序号) | int |
| `get_item_count(item_id, timeout=8)` | **获取指定物品 id 的数量**：发 `42399(MULTI_ITEM_LIST)` `[1,物品id]`，取应答包体(不含命令号)的**第 3 个参数**(索引2) | `item_id`(物品id) | int |
| `buy_item(item_id, count=1, timeout=8)` | **用赛尔豆购买指定物品 id 的数量**（药水/胶囊）：发 `2601(ITEM_BUY)` 包体 `[物品id, 数量]`（各 int32 大端，8 字节）。游戏内**买药水/胶囊**即此命令（反编译 `DrugBuyPanel`：`send(CommandID.ITEM_BUY, itemId, count)`）；需 `count×单价≤赛尔豆` 否则服务器拒绝 | `item_id`(物品id)、`count`(数量) | `Packet` |
| `get_map_players(timeout=8)` | **拉当前地图所有玩家**：发 `2003 LIST_MAP_PLAYER`（空请求），按 `UserInfo.setForPeoleInfo` 逐字段解析 → 每个玩家 `{userID,nick,pos,fireBuff,teamID,…}`。**`fireBuff`=绿火等级（0=无火；实测地图上常为 5）；`userID` 供借火**（真实 33 人应答逐字节验证：`decorateList` 恒为 5 条，`_loc15_` 不作上界） | 无 | `list[dict]` |
| `borrow_fire(uid, timeout=8)` | **向指定玩家借火**（`4292 FIRE_ACT_COPY`）：包体 `[uid:int32]` | `uid`(玩家米米号) | `Packet` |
| `auto_borrow_fire(target_fire=DEFAULT_BORROW_FIRE, *, max_borrow=1, exclude_self=True)` | **借火自动脚本一步**：`get_map_players()` → 挑 `fireBuff==target_fire`（**默认借绿火 `5`**；`None`=取地图最常见非0火）→ 排除自己 → 逐个 `borrow_fire` | `target_fire`(int 或一组值)、`max_borrow`、`exclude_self` | `{total,target,candidates,borrowed}` |
| `set_bag(ids)` | 把背包**全部**切换为指定**物种id**列表，物理重排 12 格（前6=出战，后6=待命）；读背包+仓库+**精英背包**→全部存仓库(2304)→按列表从仓库/精英取回(2304)→设首发(2308)→摆正顺序(41462) | `ids`(物种id列表，≤12) | `{"ok":True,"target":ids}` |
| `find_pet(ids)` | **查找**指定物种 id 是否存在及所在位置，在**背包(出战/待命)+仓库+精英背包**三类来源中搜索（精英背包=2361 GET_LOVE_PET_LIST） | `ids`(id 或 id 列表) | `{str(id):{"locations":[位置...],"count":n}}` |

> ⚠️ `set_bag()` 会发**真实游戏命令**（2304/2308/41462）；同一物种取池里（背包/仓库/**精英背包**）第一个未用之的；若某些物种三处都**检测不到**，则输出 `找不到指定的精灵：名称[ID=xx]，名称[ID=yy]...！`（列出**全部**缺失精灵）并`SystemExit(1)` **中止脚本**（未改动背包）；列表不足 12 时多余格位留空。`find_pet()` 是**只读查询**（含精英背包）。`set_bag()` 会把目标精灵从普通仓库或**精英背包**取出进包（精英背包与仓库一致地用 2304，对齐 WebUI 精英仓库的拖拽交互）。

- 参数列表打包对齐后端 `pack_body`：数字→int32 大端；`s:文本`/`b:字节`/`h:hex`/`bytes` 均支持；`None` 自动跳过。
- 未登录/参数错/超时/越界一律抛 `SeerError`。
- `pack_body`/`decode_body` 见 `seer/body.py`；`Packet`/`SeerError`/`Seer` 均在 `PySeer.py`。

### 后端配套
- on_frame 记录每个命令最近 RECV：`_RECV_LATEST`/`_RECV_SEQ`（脚本库用于判断“新响应”）。
- 新增 `/api/send-recv`：发包 → 等到该 cmd 出现**新的** RECV（序号变化）→ 返回完整包体 + ints。

### 对战体（`Battle`）—— 脚本驱动整场对战

`PySeer` 新增 **`Battle`（对战体）** 类，用于**脚本按回合推进一场对战**。它以
**带 `cmdid` 的完整 HEX 包**作为对战输入（只须这一个包，无需事先知道命令号语义），
随后进入回合循环：每回合可读取**当前回合数据**，也可执行**各种操作**（发包/用技能/
换宠/用道具/逃跑…），甚至用任意复杂的 `if/else/while` 判断结构来驱动决策。
收到**结束包（2506 FIGHT_OVER）** 后 `finished` 置 `True`，循环自动终止。

```python
from PySeer import Battle
battle = Battle("带cmdid的完整HEX包")   # 发送对战包 + 自动进场; 无法进入时抛 SeerError
while not battle.finished:
    my, other = battle.my, battle.other  # 双方当前出战精灵
    if my and (my.get('hp') or 0) <= 0:        # —— 任意复杂的判断结构 ——
        battle.change_pet(battle.my_team[1]['catchTime'])  # 死亡切换, 不消耗回合
        battle.use_skill(battle.skills[0])                  # 同一回合内继续出招
    elif my and (my.get('hp') or 0) < 300:
        battle.use_item(300014)                # 用药回血(消耗一回合)
    else:
        battle.use_skill(battle.skills[0])     # 使用技能(消耗一回合)
    rnd = battle.round                         # 本回合(2505)数据
    print(rnd.get('first', {}).get('lostHP'))  # 例如读取本回合伤害
```

> **操作即回合**：每个会消耗回合的操作（`use_skill`/`use_item`/`capture`/`escape`）在发包后都会
> **自动等待本回合结算(2505)** 并返回，所以脚本里**不用再写** `wait`/`wait_round`。只有**死亡切换**
> `change_pet` 不消耗回合——它把新精灵换上并更新 `my`/`skills`，之后可在**同一个回合内**继续出招。

它与 `Seer.send/recv` 的不同：`send/recv` 是**命令级**（发一条、等一条），`Battle` 是
**对局级**的会话抽象，封装了“出战队伍(2503)→开场(2504)→逐回合(2505)→结束(2506)”的整套
推进逻辑，并让脚本在回合间注入决策。

| 成员 | 说明 |
|---|---|
| `Battle(hex, ...)` / `start(hex)` | 发送带 `cmdid` 的完整 HEX 包进入对战，并**充分等待对战成功发起**（等 `active`＋双方当前精灵，且该状态**连续稳定约 0.8s** 无回退才返回，防止只收到 2503 队伍或瞬态就误判）；无法进入时抛 `SeerError`；若后端本就在对战中则直接返回当前状态、不再重复发触发包 |
| `wait(t)` | 低级原语：阻塞直到对战状态变化（`version` 递增）或结束；返回快照，超时返回 `None` |
| `wait_active(t)` / `wait_round(t)` | 低级原语：等进场 / 等一回合结果(2505)（通常无需手动调用，action 已自动） |
| `state` | 当前对战快照 `{active,finished,mode,my,other,myTeam,otherTeam,...}` |
| `my` / `other` | 我方/敌方**当前出战精灵**（结束前/结束后都可读，结束后保留最后一帧） |
| `my_team` / `other_team` | 双方出战队伍 |
| `skills` | 我方当前可用技能 id 列表 |
| `round` | **当前回合数据**：2505 `parse_note_use_skill` 结果（`first`/`second`/`hpUpdates`/…） |
| `report` / `events` | 后端战报 / 本对象观察到的事件记录 |
| `finished` | 是否已收到结束包（2506），对战体据此终止 |
| `send(cmd, params)` / `send_hex(hex)` | 任意发包（命令名或命令号 / 原始 HEX 包），**不自动等回合** |
| `use_skill(sid)` | 用技能(2405)，发包后**自动等本回合结算(2505)**，消耗一回合 |
| `use_skill_smart(sid, *, pp_potion_id=300017, refill=True)` | **出招前检查该技能 PP**：`最大PP>0`(取自 `data/skills.json` 的 `pp`)且 `当前PP==0`(取自后端每回合 `mySkillPP`) 时，用**中级活力药剂(300017)** 回复后再出招；没货先 `buy_item` 买1份再 `use_item` 喝，有货先 `use_item` 喝再 `buy_item` 补回1份。其余情况直接出招 |
| `use_item(item_id, catchTime=None)` | 用道具(2406)，包体 `[我方catchTime, 物品id, 0]`，`catchTime` 默认取当前出战精灵；发包后**自动等本回合结算**，消耗一回合 |
| `capture(...)` | 捕捉(2409)，发包后**自动等本回合结算**，消耗一回合 |
| `change_pet(id)` | **换宠**(2407)：传**物种 id**，后端从当前对战阵容(`myTeam`)查一只可用该 id 精灵取 `catchTime` 发包（也可 `change_pet(None, catchTime=…)` 直接指定）。然后**等到新精灵真正成为我方当前出战**(`my.catchTime` 变化，可能被后续 2505 覆盖 `lastCmd` 或对端换宠不更新 `my`，因此不等 `lastCmd==2407`)。`death=None` 自动判断——当前精灵阵亡→**死亡切换**(不消耗回合，换完可继续出招)；还活着→**主动切换**(消耗一回合，等 2505)；`death=True/False` 可强制 |
| `escape()` | 逃跑(2410)，发包后**自动等对战结束(2506)** |
| `skill_pp(sid)` | 取该技能**当前剩余 PP**（服务器每回合 2505 经 `mySkillPP` 同步，权威值）；未同步返回 `-1` 表示未知（未知≠0，不误判为耗尽） |
| `act(msg)` | 把一条脚本动作记入后端战报（便于观察/回放） |
| `run(decide, t)` | **自动驱动**整场对战直到结束包：循环调用 `decide(this)`（回调里写复杂判断并出招），每个动作自动等回合，收到 2506 返回 `True` |

> 模块级：`skill_max_pp(sid)` 按技能 id 查**最大 PP**（`data/skills.json` 的 `pp`，查不到返回 0）；`PP_RESTORE_ITEM=300017` 为**中级活力药剂**（恢复技能 PP）物品 id，`use_skill_smart` 默认用它。`use_skill_smart`/`skill_pp` 的完整说明见 `docs/PySeer.md`「智能出招」一节。

**后端配套**：`_BATTLE` 新增 `finished` 字段（收到 2506 置 True；新一轮 2503、`/api/battle/clear`、
**`/api/battle/hex`（发起对战）** 都会复位为 False），并在
进入新回合时清掉上一场 `lastSkill`（防止跨场误读当前回合）；新增 **`POST /api/battle/wait`**
（参数 `{version, timeout}`）：**长轮询**阻塞到 `_BATTLE.version` 递增或 `finished`，返回最新快照，
供脚本按“下一个事件”推进。`wait` 的 `version` 语义与 `_BATTLE.version`（每处理一个对战包递增）一致。

> 关于“二次运行脚本即报错”：上一场对战结束后 `finished` 会留在 True，若新一场触发前未清除，
> 脚本端等待本场 2503 时会把它误判成“收到结束包”。已通过 **发起对战(`/api/battle/hex`)时复位
> `finished`** 以及 **`Battle._wait_entry` 在本场进入前忽略遗留 `finished`** 双重修复。

> 说明：`capture`/`escape` 默认发送**空包体**（与 WebUI 页面按钮行为一致）；若需自定义
> 包体请用通用 `send(cmd, params)`。对战命令号 `2405/2406/2407/2409/2410/2503/2504/2505/2506`
> 见 `app/cmdmap.json` 与 `docs/REPRODUCTION.md`。

> ⚠️ **2406 用药的包体是三个 int32**：`[我方当前出战精灵 catchTime, 物品id, 0]`
> （依据客户端 `refs/.../data/item/RenewBloodItemCategory.as` 的
> `send(USE_PET_ITEM, playerMode.info.catchTime, itemID, 0)`）。
> **只发物品 id 会被服务端判为非法操作** —— 实测立刻回 `2506 FIGHT_OVER` 并**断开连接**。
> `use_item()` 已自动补 `catchTime`。

---

## 6. 目录结构（与本成果相关）

```
seer-login-test/
├── app/                      # 程序文件 (运行必需, git track)
│   ├── webui.py              # 协议调试台 WebUI (http://127.0.0.1:8680): 精灵详情/专属特性/技能/拖拽换位
│   ├── PySeer.py            # 脚本第三方库: Seer.send/recv/get_value
│   ├── assets_updater.py     # 自更新数据管线: 下载+解析 petbook/monsters/skilltypes/moves/skill_effect/effecticon/effectag
│   ├── login_test.py         # 登录协议自检/测试入口
│   ├── mock_server.py        # 模拟网关 (不联网自检用)
│   ├── cmdmap.json           # 命令 id -> 命令名
│   ├── requirements.txt
│   ├── seer/                 # 协议客户端包
│   │   ├── body.py           # pack_body / decode_body / parse_parts
│   │   ├── petinfo.py        # PetInfo 各段解析 (依据 refs/*.as)
│   │   └── ...               # client/session/tcp_client/ws_client/packet/algorithm/misc/fightinfo
│   └── scripts/              # "脚本"页默认脚本存放目录 (用户把 .py 放进来即可在页面运行)
├── data/                     # 运行时下载/生成的资源 (gitignore)
│   ├── head/                 # 精灵头像(<物种id>.png)
│   ├── effecticon/           # 魂印/效果图标(按 icon_id 命名)
│   ├── petbook.json          # 精灵 id -> 名字 (自更新)
│   ├── pet_attr.json         # 精灵物种 id -> 属性中文名 (自更新)
│   ├── skills.json           # 技能 id -> 技能数据 (自更新)
│   ├── soulmarks.json        # 精灵 id -> 专属特性魂印列表 (自更新)
│   └── webui_*.json          # 运行时凭据/过滤/后端地址 (gitignore)
├── refs/                     # 逆向参考资料 (gitignore)
│   ├── *.as                  # 反编译的 PetInfo/PetSkillInfo/PetEffectInfo/PetResistanceInfo/FightInfo 等
│   ├── captured              # 6v1 战斗抓包 (2505 NOTE_USE_SKILL 解基准)
│   ├── gamedump4.txt         # 游戏本体抓包
│   ├── monsters.json/.txt    # 早期静态参照 (已被自解析取代)
│   ├── seerNew/ seerpacket/ Petbag/  # 他人逆向实现/反编译参考
│   └── seerpacket/cmdmap.json # Command.cs 解析的命令表 (cmdmap.json 的来源)
├── analysis/                 # 分析工具与产物 (gitignore)
│   ├── analyze_gamedump.py   # 抓包离线分析器
│   ├── extract_pet_heads.py  # 从 bundle 解精灵头像入口
│   ├── crack_seed*.py        # 离线反推 gamedump4 会话密钥
│   └── gamedump4_*.txt/csv、postlogin_*.txt、cracked_session.txt 等产物
├── cache/                    # 下载缓存 (get-pip.py / 各 .bundle, gitignore)
├── vendor/                   # 本地 pip 用具 (UnityPy/pip_tool, gitignore)
├── webui_logs/               # 运行日志 (gitignore)
├── docs/                     # 文档
│   ├── DEVELOPMENT.md        # 开发成果整理 (本文)
│   └── REPRODUCTION.md       # 给 AI/复现者 的技术复现要点
└── README.md                 # 项目介绍/快速上手
```

---

## 7. 数据文件说明

- **petbook.json**：`{"<物种id>": "<名字>"}`，来源于 petbook.bytes，供界面回填名字。
- **pet_attr.json**：`{"<物种id>": "<属性名>"}`，如 `"1":"草"`, `"502":"水 龙"`，来源于 monsters.bytes 基表 `type` 字段。
- **skills.json**：`{"<技能id>": {name,pp,type,typeName,power,accuracy,crit,mustHit,priority,effects:[{id,args,desc,tag}],info}}`，来源于 moves.bytes + skill_effect.bytes。
- **soulmarks.json**：`{"<物种id>": [{id,tags,desc,analyze,effectId,args,iconId}]}`，来源于 effecticon.bytes + effectag.bytes；`effectId` 用于与精灵 `effects` 的 `effectID` 匹配出当前阶段。

---

## 8. 常用启动/验证

```bash
# 启动 WebUI(后台, 已登录后生效); 在项目根目录运行
PYTHONPATH=vendor/unitypy nohup python3 -u app/webui.py --port 8680 >/tmp/webui8680.log 2>&1 &
# 浏览器打开 http://127.0.0.1:8680/

# 手动触发数据更新(下载+解析全部)
PYTHONPATH=vendor/unitypy python3 app/assets_updater.py --force

# 脚本库自检(需后端已登录; 会刷背包并打印 43706 包体)
PYTHONPATH=vendor/unitypy python3 -m app.PySeer
```

> 说明：`app/` 里 `webui.py`/`PySeer.py`/`assets_updater.py` 都通过 `os.path.dirname(__file__)`
> 推导出 `app/` 目录，再取上一级为项目根、`data/` 为运行时数据目录；因此**从项目根或 `app/` 目录运行均可**，
> 只要 `PYTHONPATH` 含 `vendor/unitypy`。

---

## 9. 待办/可扩展

- 背包↔背包移动命令（仓库→背包用 2304 已确定；背包↔背包用 41462 目标空位 catchTime=0 为推断，需实测确认）。
- 更立体的“技能/专属特性”富文本（`analyze` 中的颜色/图标标记）渲染。
- 在 `PySeer.py` 基础上封装更高层 API：读背包（`set_bag`/`find_pet`）与**战斗**（`Battle` 对战体，
  见上）已有；后续可再封装“自动练级/刷BOSS”等复合策略（需真实对战触发 HEX 包）。
- `refs/monsters.json`/`refs/monsters.txt` 已基本被自解析取代，仅作参照。

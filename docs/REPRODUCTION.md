# 赛尔号通信协议技术复现 — 面向 AI / 二次开发者的速查与复现手册

> 本文档把本项目在**协议逆向**上已确认的事实，整理成一份**可直接照抄复现**的技术手册。
> 目标是：让一个没有上下文的 AI 或工程师，仅凭本文 + 仓库里 `app/seer/` 源码，
> 就能**重新实现登录、发包、背包含义解析、精灵详情解析、对战回合解析**，而不必从头逆。
>
> 事实来源：仓库内反编译的 `refs/seerpacket/*.cs`、`refs/seerNew/`，52pojie
> `thread-1468888` / `thread-2053139` 的逆向结论，以及本项目**实抓包实测**确认的结构。
> 相关文档见 [README.md](../README.md)（项目介绍）与 [DEVELOPMENT.md](./DEVELOPMENT.md)（开发成果）。
>
> ⚠️ **使用边界**：仅用于对**你自己拥有**的赛尔号账号做协议学习与验证；勿用于批量登录、
> 盗号、凭证窃取或违反游戏服务条款。

---

## 0. 目录速览（从哪里看什么）

| 想看什么 | 去看 |
|---|---|
| 登录流程 / 会话密钥派生 | `app/seer/client.py`、`app/seer/packet.py`、`app/seer/session.py` |
| 封包加解密算法 | `app/seer/algorithm.py`、`app/seer/packet.py` |
| 封包结构（长度前缀/ver/cmd/uid/res/body） | `app/seer/body.py`、`app/seer/packet.py`、`refs/seerpacket/SendPacket.cs` |
| 精灵信息段解析 | `app/seer/petinfo.py`（依据 `refs/*.as`、`refs/petinfo/`） |
| 战斗相关包（2503/2504/2505/2506/2404/2407/…） | `app/seer/fightinfo.py` |
| 命令号 map | `app/cmdmap.json`（由 `refs/seerpacket/Command.cs` 生成） |
| 运行时数据（名字/属性/技能/魂印） | `app/assets_updater.py` 产出到 `data/*.json` |
| 抓包离线分析器 | `analysis/analyze_gamedump.py` + 产物 `analysis/gamedump4_*.txt/csv` |

---

## 1. 封包结构（wire 与明文）

### 1.1 加密封包（wire）

游戏服务器是**裸 TCP**（默认 `101.43.19.60:1201`）。每个封包在字节流上自描述：

```
wire = [4B 大端总长度(含这4字节)][密文]
```

- 密文是**位移 + XOR**，密钥 `!crAckmE4nOthIng:-)`（登录前默认密钥）。
- 登录（cmd=1001）响应后，客户端按规则派生**会话密钥**并切换，之后所有封包用它解密。
- 加解密在 `app/seer/algorithm.py` 实现（`Decrypt` / `Encrypt`，模块级 `_key`）。

### 1.2 明文（解密后）

```
plain = [ver(1B)][cmd(4B 大端)][uid(4B 大端)][res(4B 大端)][body...]
```

**ver 字节 = 方向/形态**（`app/seer/client.py` 常量）：

| ver | 含义 | 命令常数 |
|---|---|---|
| `0x31` | 客户端请求 (SEND) | `VER_CLIENT_REQ` |
| `0x01` | 服务器主动推送 (NOTE) | `VER_SERVER_NOTE` |
| `0x3E` | 服务器对某条请求的应答 | `VER_SERVER_RESP` |

判别函数 `_ver_kind()` 据此返回 `request / note / response / unknown`。
`recv_game_packet()` 返回带 `kind` 的解析结果；需要"等某条应答、跳过推送"时用
`recv_until(cmd)`，它只在 `cmd==目标 && kind!='note'` 时停下。

> ⚠️ **易错点**：同一 cmd 可能是 NOTE 也可能是 RESP（如 2503/2505 是 NOTE，
> 2504/2404 是 RESP）。所以**收到包要先用 ver 判方向**，再决定按哪个结构解。

---

## 2. 登录流程与会话密钥

完整流程（`app/seer/client.py::login_game`）：

1. 连接网关，得到 `ws://host:port`（或直接裸 TCP 到 `DEFAULT_GAME_SERVER`）。
2. 发送登录封包：`cmd = 1001 (0x3E9)`，包体 =
   `session(16B) + GAME_LOGIN_TAIL`（`GAME_LOGIN_TAIL` 长为 108B，含 `"flash_taomee"` 等）。
3. 等服务器 `cmd=1001` 且包体较大（>~100B，即角色数据，约 7KB）。
4. 从该响应明文派生**会话密钥**（`derive_session_key`）：

```
seed = LOGIN_IN 响应明文最后 4 字节 (大端 uint)
xor  = seed ^ 米米号(uid)
key  = md5( str(xor) ).hexdigest()[:10]     # 取 md5 前 10 个字符
```

5. 之后所有收发包用 `key` 加解密；心跳 `cmd = 0x3EA` 保活。
6. 登录成功后拿 `session_key`，后续 `send_game_packet(cmd, body_hex)` 发包。

> 密钥派生这条规则来自参考帖；若服务器版本更新导致失效，需重新实测。

---

## 3. 命令号（cmdmap）

`app/cmdmap.json` 是 `{命令号(十进制字符串): 命令名}`。
命令号范围：`>=1000` 的封包才用 seer 加密；`<1000`（如网关握手）不走这套。
反向映射 `CMD_NAME`（名 → id）也常用。

常见命令号（见 `app/cmdmap.json` / `refs/seerpacket/Command.cs`）：

| cmd | 名字 | 说明 |
|---|---|---|
| 1001 | LOGIN_IN | 登录 |
| 0x3EA | (心跳) | 保活 |
| 2301 | GET_PET_INFO | 精灵详情（主导） |
| 43706 | GET_PET_INFO_BY_ONCE | 整批查背包精灵 |
| 2304 | PET_RELEASE | 释放/取出精灵 |
| 2051 | GET_SIM_USERINFO | 用户信息 |
| 2503 | NOTE_READY_TO_FIGHT | 出战队伍（NOTE） |
| 2504 | FIGHT_START | 开局双方当前精灵（RESP） |
| 2505 | NOTE_USE_SKILL | 回合结果（NOTE） |
| 2506 | FIGHT_OVER | 对战结束（RESP） |
| 2404 | READY_TO_FIGHT | 准备就绪 |
| 2405 | USE_SKILL | 使用技能 |
| 2406 | USE_PET_ITEM | 用药 |
| 2407 | CHANGE_PET | 换宠 |
| 2409 | CATCH_MONSTER | 捕捉 |
| 2410 | ESCAPE_FIGHT | 逃跑 |
| 2394 | PET_BOOK_UPDATE | 图鉴更新 |

---

## 4. 精灵信息段解析（`app/seer/petinfo.py`）

精灵信息（PetInfo）由**多段**串成，依据 `refs/*.as`（反编译）推断字段顺序。
字段多为 `readUnsignedInt`（大端 u32）/ `readUTFBytes(16)`（名字，16B）混合。
关键点：

- **id 与 名字**：`id` 是物种 id；名字字段服务端常**留空**，需从本地
  `data/petbook.json`（`{"物种id":"名字"}`）查。
- **属性/天赋/等级/性格/六维**：按段解析，例（实测 2301 包体）：
  `id=5000 等级=100 天赋=31 性格=8 体力=584/584 攻击=230 防御=276 特攻=439 特防=277 速度=316`。
- **精灵背包整体**（43706）：`split_petbag_43706` 解出 `first_count`/`second_count`
  及每只精灵（`id=4648/3577` 等）。

> 复现时：优先用 `parse_full(body)` 解单只，用 `split_petbag_43706` 解整包。

---

## 5. 战斗包解析（`app/seer/fightinfo.py`）— 重点

战斗是一个**回合制**流程，命令号 + ver 决定结构。下面顺序即一次真实对战的推进。

### 5.1 2503 NOTE_READY_TO_FIGHT（出战队伍）
`NoteReadyToFightInfo`：`[mode(u32)][efFightType(u32)] + 2 × FighterUserInfo`。
- `mode` 在 `SPECIAL_MODES`（`{14,36,37,44,…112}`）里时，每只宠物读**完整 PetInfo**；
  否则读**浓缩战斗数组** `[catchTime][id][hp][技能数][技能…][效果数][效果…][skinId]`。
- 两个用户按 `id==当前账号` 区分我方/敌方。

### 5.2 2504 NOTE_START_FIGHT（开局双方当前精灵）
`FightStartInfo`：`[isCanAuto(u32)][isShowFightHp(u32)] + 2 × FightPetInfo`。
- `FightPetInfo` 含：`userID, petID, petName(16B), catchTime, hp(int), maxHP, lv, catchType,
  resistance(56B), skinId, changehps[…], requireSwitchCthTime, xinHp, xinMaxHp, isChangeFace,
  secretLaw, skillRunawayMarks[…], holyAndEvilThoughts, yearVip2022_*, siteBuff(3B),
  bothSiteBuff(3B), markBuff, signInfo[…], lockedSkillArr(5×u32)`。
- `FightSignInfo`(8B)：`id(16b)|lvNum(8b)|roundNum(8b)` + `spValue(u32)`。
- 我方/敌方由 `userID==actorID` 决定；`requireSwitchCthTime` 用于"自动换宠"。

### 5.3 2505 NOTE_USE_SKILL（回合结果）—— 最容易踩坑
**一个 2505 = 一整回合**，内含**多条同构的 16B 技能记录**：

```
技能记录(16B) = [userID(u32)][skillID(u32)][count(u32=回合数)][actorCatchTime(u32)]
```

- **我方技能**在包体**前导**(offset 0)。
- **敌方技能**是**内嵌在包体深处、byte 级偏移(不按 4 对齐)** 的同级子块 —— 必须**逐字节扫描**
  `extract_skill_use_records()` 才能把双方本回合技能都解出来（敌方子块位置每次不同：
  实测 412 / 448 / 573B 等，取决于前面变长字段长度）。
- 敌方子块 `userID==0`，我方子块 `userID==当前账号`。
- **HP 更新记录**两种布局（`extract_pet_hp_updates` 兼容）：
  - 格式A(相邻三段式)：`[catchTime][hp][maxhp]`
  - 格式B(32B)：`[catchTime][type][1][val][0][0][hp][maxhp]`（hp 在 +24，maxhp 在 +28）
  - `hp==0` 表示**阵亡**；守卫 `maxHp>1` 过滤假记录 `[ct][0][1]`。

实测（打谱尼 Boss，mode=67）：敌方技能 `10995 旋灭裂空阵(圣灵·135)`、
`20500 圣光气(普通·0·必中，属性/回复类)`；我方 `37381 星光·浪打千击(水·30·必中)`。
敌方技能在**斩杀回合(敌方阵亡)** 往往不再出现。

### 5.4 2506 FIGHT_OVER（对战结束）
`FightOverInfo` 包体 **57B 几乎全 0**，有效字段仅：
- `offset 5 (u32)`：我方账号 id
- **末字节**：本场**回合数**（`0x03`=3回合、`0x02`=2回合、`0x00`=未打/取消）

**不含胜负标志**。胜负靠本场最后一次 2505 的 HP 更新判定：
- 敌方HP==0 且我>0 → **我方胜利**
- 我方HP==0 且敌>0 → **我方战败**
- 双双归零 → 同归于尽
- 都无法判定（如取消场）→ 胜负未判定

### 5.5 已确认的其它战斗命令
| cmd | 含义 | 解析 |
|---|---|---|
| 2404 | 准备就绪 | 空包体；服务器回 2503 后客户端应自动发一条，否则不出回合 |
| 2405 | 使用技能 | —— |
| 2406 | 用道具 | `UsePetItemInfo`：userID/itemID/userHP/changeHp/round |
| 2407 | 换宠 | `ChangePetInfo`（结构与 FightPetInfo 相近） |
| 2409 | 捕捉 | `CatchPetInfo`：catchTime + petID |
| 2410 | 逃跑 | —— |

---

## 6. 抓包离线分析（`analysis/analyze_gamedump.py`）

`refs/gamedump4.txt` 是真机抓包（**UTF-16LE**，Tab 分隔），用来在**不联网**下复原协议。
流程（`analysis/analyze_gamedump.py`）：

1. 读文件，按 Tab 拆 `(序号,时间,srcIP,dstIP,srcPort,dstPort,hex)`。
2. 按连接 5 元组分组，对每个方向**去冗余 + 拼接**（抓包工具重复/覆盖记录同一段数据）。
3. 用 **4B 大端长度前缀**切帧。
4. 解密（登录前默认密钥；收到 1001 角色数据后切会话密钥）。
5. 判定方向：cmd==1001 且包体大 → S2C 角色数据；包体小 → C2S 登录；乱码命令号剔除。
6. 用 `seer/body.py::decode_body` 切成十进制数组，用 `cmdmap.json` 映射命令名。

产物：`gamedump4_decoded.txt`（逐条）、`gamedump4_named.txt`（命令统计）、
`gamedump4_cmds.txt`（`cmd/hex/ints`）、`gamedump4_cmds.csv`（带 UTF-8 BOM）。

> 密钥反推（`analysis/crack_seed*.py`）：缺 1001 响应时用**已知明文攻击**——
> 登录后明文头 `[ver=0x31][cmd][uid(米米号)][res]`，对候选 seed 算 key 解一个登录后包，
> `ver==0x31 且 uid==account` 即正确。

---

## 7. 运行时数据管线（`app/assets_updater.py`）

WebUI 的精灵名字/属性/技能/魂印全部**从游戏资源自更新**，不依赖静态他人表：

| 产物(在 `data/`) | 数据源(bundle 内) | 解析 |
|---|---|---|
| `petbook.json` | `petbook.bytes` | `extract_petbook_names` |
| `pet_attr.json` | `monsters.bytes` + `skilltypes.bytes` | `parse_monsters`/`parse_skill_types` |
| `skills.json` | `moves.bytes` + `skill_effect.bytes` | `parse_moves`/`parse_skill_effects` |
| `soulmarks.json` | `effecticon.bytes` + `effectag.bytes` | 魂印 |
| `head/*.png` | DefaultPackage `_pet_head_*.bundle` | `extract_pet_avatars` |
| `effecticon/*.png` | DefaultPackage `_effecticon_*.bundle` | `ensure_effect_icons` |

每个产物有**版本状态文件**（`data/.xx_state.json`），命中版本即跳过。
UnityPy 若不是标准库，会自动 `pip install` 到 `vendor/unitypy`（不污染系统）。

---

## 8. 最小复现路径

```bash
# ① 启动 WebUI（项目根目录；PYTHONPATH 需指向 vendor/unitypy）
PYTHONPATH=vendor/unitypy nohup python3 -u app/webui.py --port 8680 >/tmp/webui8680.log 2>&1 &
# 浏览器 http://127.0.0.1:8680/

# ② 手动刷新游戏数据
PYTHONPATH=vendor/unitypy python3 app/assets_updater.py --force

# ③ 脚本库自检（后端已登录后；会刷背包并打印 43706 包体）
PYTHONPATH=vendor/unitypy python3 -m app.seerlib

# ④ 离线分析抓包（不联网）
python3 analysis/analyze_gamedump.py --out-decoded /tmp/d.txt --out-named /tmp/n.txt
```

---

## 9. 关键易错点（复现踩坑清单）

1. **先判 ver 再解码**：2503/2505 是 NOTE，2504/2404 是 RESP；乱套会解错/状态错乱。
2. **敌方技能不在前导**：2505 前导永远是我方 skill；敌方技能是深处 byte 级未对齐子块，
   必须逐字节扫 `extract_skill_use_records()`。
3. **HP 记录两格式**：32B 格式的 hp/maxhp 在 +24/+28；`hp==0`=阵亡；`maxHp>1` 过滤假记录。
4. **2506 无胜负标志**：末字节是回合数；胜负从最后一次 2505 的 HP 推。
5. **会话密钥**：登录后必须切换，否则后面全解错。
6. **名字字段常空**：需从 `data/petbook.json` 回填。
7. **登录后要先等 2503 → 回 2404**，否则不进入出招回合。

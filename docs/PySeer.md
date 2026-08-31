# PySeer — 赛尔号 Python 脚本库（完整说明）

> `PySeer.py` 是供脚本使用的**第三方库**：只需标准库（`urllib`），对接 **PySeer 后端（`webui.py`）** 的 HTTP 接口，
> 让脚本驱动已登录的游戏账号发/收包、查/改背包、以及**自动按回合驱动对战**。
> 相关文档：[README](../README.md)、[DEVELOPMENT](./DEVELOPMENT.md)、[REPRODUCTION](./REPRODUCTION.md)。

---

## 目录

1. [前置条件与运行](#1-前置条件与运行)
2. [后端地址自动发现](#2-后端地址自动发现)
3. [基本类型（Packet / SeerError）](#3-基本类型packet--seererror)
4. [包体参数语法（spec）](#4-包体参数语法spec)
5. [`Seer`：命令级发/收/取、背包操作](#5-seer命令级发收取背包操作)
6. [`Battle`：对战体（自动按回合驱动）](#6-battle对战体自动按回合驱动)
7. [常用命令号速查](#7-常用命令号速查)
8. [完整示例](#8-完整示例)
9. [注意事项与边界](#9-注意事项与边界)

---

## 1. 前置条件与运行

`PySeer` 本身不含连接逻辑，它通过 HTTP 调用后端。因此需要：

1. **后端已启动并登录**：在项目根目录运行
   ```bash
   PYTHONPATH=vendor/unitypy nohup python3 -u app/webui.py --port 8680 >/tmp/webui8680.log 2>&1 &
   ```
   并在 WebUI（`http://127.0.0.1:8680/`）登录一个你拥有的游戏账号。
2. **脚本能 import PySeer**：把项目根目录或 `app/` 放进 `PYTHONPATH`：
   ```bash
   PYTHONPATH=app python3 你的脚本.py
   ```
   或把脚本放进 `app/scripts/`，在 WebUI「脚本」页选择运行（后端已自动处理路径）。

> ⚠️ **使用边界**：仅用于对**你自己拥有**的账号做协议学习与验证；`set_bag()`、对战等会发**真实游戏命令**，
> 有账号冻结风险，请用好找的/可恢复的数据测试。

---

## 2. 后端地址自动发现

`PySeer` 自动定位后端，脚本**不必硬编码地址**：

```python
from PySeer import Seer
s = Seer()          # 交给 discover_backend() 自动找
```

`discover_backend(explicit=None, probe=True, timeout=1.0)` 按优先级：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `Seer(base=...)` 显式传入 | 最高优先，不做探测 |
| 2 | 环境变量 `SEER_BACKEND` | 显式覆盖，不做探测 |
| 3 | `data/webui_addr.json` | 后端启动时写入的**实际监听地址** |
| 4 | 逐端口探测 `8680..8699` | 用 `/api/status` 判定存活，覆盖 `--port 0`/换端口后仍在线的实例 |
| 5 | 兜底 `http://127.0.0.1:8680` | |

> 当 `webui_addr.json` 指向的后端已下线，会自动回退到仍在线的实例。因此 `Seer()` 不传参即可。
> 若想固定地址：`Seer("http://127.0.0.1:8680")`。

---

## 3. 基本类型（`Packet` / `SeerError`）

### `SeerError`
`PySeer` 里的异常统一用 `SeerError` 抛出：未登录、参数错误、等待响应超时、取值越界、后端返回错误等。

```python
from PySeer import SeerError
try:
    s.recv(2405)
except SeerError as e:
    print("失败:", e)
```

### `Packet`
`recv()` 的返回对象，描述一条 RECV 包体：

| 成员 | 类型 | 说明 |
|---|---|---|
| `body` | `str` | 完整包体十六进制 |
| `ints` | `list[int]` | 按 4 字节大端 int32 拆出的十进制列表 |
| `raw` | `bytes` | 二进制包体 |
| `cmd` | `int` | 命令号 |
| `pkt[i]` / `pkt.get(i)` | `int` | 取第 i 个 int32 |

```python
pkt = s.recv(43706)
print(pkt.body, pkt.ints[:5], pkt.raw.hex(), pkt[0])
```

---

## 4. 包体参数语法（spec）

`send`/`recv` 的 `params` 说明“包体怎么组装”，支持（可混用，逗号或空格分隔）：

| 写法 | 含义 | 例 |
|---|---|---|
| 裸数字 / `i:N` | int32 大端（支持 `0x` 前缀、负数补码） | `0 10 725 172` |
| `b:N` | 单字节（0..255） | `b:255` |
| `h:HEX` | 原始十六进制字节 | `h:010203` |
| `s:文本` | 1 字节长度 + UTF-8 文本 | `s:abc` |
| `bytes` 对象 | 自动转成 `h:<hex>` | `b"\x00\x01"` |
| `None` | 跳过（占位） | — |
| 空串 / `None` 参数 | 空包体 | `send(43706)` |

> 官方约定：`ENTER_MAP(2001)` = `[0][地图号][x][y]`，输入 `0 10 725 172` → `00 00 00 00 00 00 00 0A 00 00 02 D5 00 00 00 AC`。

---

## 5. `Seer`：命令级发/收/取、背包操作

```python
from PySeer import Seer
s = Seer()          # 自动定位后端
```

### `Seer(base=None, timeout=30.0, probe=True, probe_timeout=None)`
`timeout` 为 HTTP 超时。返回带 `.base`（后端地址）、`.timeout`。

### 三大函数

#### `send(cmd, params=None) -> dict`
发送一条 SEND 包（**不等待响应**）。`cmd` 可为命令号或命令名（如 `"ENTER_MAP"`、`43706`）。返回后端应答 `dict`（含 `ok`、`sent`）。

```python
s.send(43706)                 # 刷背包
s.send("ENTER_MAP", [0, 10, 725, 172])
s.send(2405, [37381])         # 用技能
```

#### `recv(cmd, params=None, timeout=8.0) -> Packet`
发送 SEND 包并**等待该命令的 RECV 应答**，返回 `Packet`。超时抛 `SeerError`。

```python
pkt = s.recv(2301, [3266, 0, 0, 0])   # 查单只精灵详情
v = s.get_value(pkt, 0)
```

> `recv` 走后端 `/api/send-recv`：发包后等到该 cmd 出现**新的** RECV（用序号区分，跳过旧响应）。

#### `get_value(body, index) -> int`
从包体取第 `index` 个 int32（大端）。`body` 可为 `Packet`/hex `str`/`bytes`。越界抛 `SeerError`。

```python
v = s.get_value(pkt, 0)
w = s.get_value("0000000100000000", 1)
```

#### `get_recv_value(cmd, params, index, timeout=8.0) -> int`
一步完成"发包 → 等该命令 RECV → 取应答包体第 `index` 个值"，等价于 `get_value(s.recv(cmd, params), index)`。`cmd` 可为命令号或命令名；`params` 为发送包体（见 spec 语法）；`index` 为应答包体（不含命令号/包头）的**参数序号**（0 基 int32 索引）。越界抛 `SeerError`。

```python
# 发 2301 查精灵详情, 直接取应答的第 0 个值
v = s.get_recv_value(2301, [3266, 0, 0, 0], 0)     # -> 首只精灵 id(或包体内第0个 int32)
# 发 42399 查物品, 取应答第 3 个参数(索引2) 即数量
n = s.get_recv_value(42399, [1, 2600048], 2)       # -> 物品数量
```

#### `get_item_count(item_id, timeout=8.0) -> int`
获取指定**物品 id** 的数量。发 `42399(MULTI_ITEM_LIST)` 包体 `[1, 物品id]`（两个 int32 大端），服务器应答包体（**不含命令号/包头**）按 int32 拆，取**第三个**参数（索引 2）即该物品数量。

```python
n = s.get_item_count(60001)     # 物品 id=60001 -> 返回数量(int)
```

> 若应答取不到第 2 个参数抛 `SeerError`；`item_id` 为物品 id（int）。

#### `buy_item(item_id, count=1, timeout=8.0) -> Packet`
用**赛尔豆**购买指定**物品 id** 的数量（如药水/胶囊）。发 `2601(ITEM_BUY)` 包体 `[物品id, 数量]`（各 int32 大端，共 8 字节）。游戏内**买药水/胶囊**即此命令——依据反编译的 `DrugBuyPanel`：
```actionscript
SocketConnection.send(CommandID.ITEM_BUY, itemId, count);   // CommandID.ITEM_BUY = 2601
```
需满足 `count × 单价 <= 当前赛尔豆`，否则服务器拒绝（客户端会先提示"赛尔豆不足"）。

```python
pkt = s.buy_item(300017, 1)        # 买 1 份中级活力药剂(物品 id=300017)
```

> 药剂面板常见物品 id：`300013` 高级体力药剂、`300014` 超级体力药剂、`300012` 中级体力药剂、`300016` 初级活力药剂、`300017` 中级活力药剂、`300002` 中级精灵胶囊、`300003` 高级精灵胶囊。返回完整 `Packet`。
> ⚠️ `buy_item`/`get_item_count` 走**非对战**通道（`/api/send-recv`），对战中能否直接购买取决于后端实现。

#### `get_map_players(timeout=8.0) -> list`
拉取**当前地图上所有玩家**（`2003 LIST_MAP_PLAYER`，空请求）。按 `UserInfo.setForPeoleInfo` 逐字段解析每个玩家，返回 `list[dict]`：

```python
players = s.get_map_players()
# [{userID, nick, pos:[x,y], fireBuff, actionType, teamID, coreCount, vip}, ...]
```

- **`fireBuff`**：对方"绿火/圣火"等级（`0`=无火；实测地图上常见 `5` 为某种火）。借火时据此挑目标。
- **`userID`**：给 `borrow_fire` 用的米米号。
- 每条记录已用真实 33 人应答做成**逐字节精确消耗**验证（含一个坑：`decorateList` 恒为 5 条，`_loc15_` 不作循环上界）。

#### `borrow_fire(uid, timeout=8.0) -> Packet`
向指定玩家**借火**（`4292 FIRE_ACT_COPY`）：请求包体 `[uid:int32]`。`uid` 来自 `get_map_players` 的 `userID`。

```python
pkt = s.borrow_fire(947177385)     # 向该玩家借绿火
```

#### `auto_borrow_fire(target_fire=DEFAULT_BORROW_FIRE, *, max_borrow=1, exclude_self=True, timeout=8.0) -> dict`
**借火自动脚本一步**：拉当前地图玩家 → 挑 `fireBuff` 符合者 → 逐个借火。

- `target_fire`：目标 fireBuff 值（int 或一组值）；**默认借绿火 `fireBuff==5`**（`DEFAULT_BORROW_FIRE`，实测地图最常见）；`None` 时自动取地图上**最常见的非 0 火**。
- 排除自己（`exclude_self`）与 `userID==0`，逐个 `borrow_fire`，最多 `max_borrow` 个。

```python
r = s.auto_borrow_fire()                            # 默认借绿火(5), 借 1 个
r = s.auto_borrow_fire(max_borrow=3)                # 借绿火, 最多 3 个
r = s.auto_borrow_fire(target_fire=8, max_borrow=2) # 借别的火(值在 FIRE_TYPES 校准)
# -> {"total":33, "target":[5], "candidates":[...], "borrowed":[...每个含 userID/nick/fireBuff/pkt或error]}
```

> 火焰类型见模块级 `FIRE_TYPES`（`{0:无火, 5:绿火}`，**已确认 5=绿火**；蓝/紫/金未实测，请按实际补充）。
> 默认借的是最常见的那种=绿火(`DEFAULT_BORROW_FIRE=5`)。
> ⚠️ `2003`/`4292` 走非对战通道（`/api/send-recv`）；借火前需先登录且**在某个地图里**（否则拉不到同屏玩家）。

### 换背包：`set_bag(ids) -> dict`
把背包**全部**切换成指定的**物种 id 列表**，**物理重排 12 格**（前 6 = 第一背包/出战，后 6 = 第二背包/待命）。

```python
s.set_bag([5000, 5001, 5002])     # 最多 12 个
```

**流程**：读当前背包 + 仓库 + **精英背包**(2361)（先校验目标物种都在，避免改一半）→ 全部存仓库(2304) → 按列表取回（前6进背包1、后6进背包2）→ 设首发(2308) → 摆正顺序(41462 交换)。

**约定 / 注意**：
- 同一物种在“背包/仓库/精英背包”任一处取第一个未用的；若某物种**三处都找不到**，输出
  `找不到指定的精灵：名字[ID=xx]，名字[ID=yy]...！` 并 `SystemExit(1)` **中止脚本**（此时背包未改动）。
- 列表不足 12 时多余格位留空。
- ❗ 会发**真实游戏命令**（2304/2308/41462），请用安全/可恢复的列表测试。

### 查找精灵：`find_pet(ids) -> dict`
`ids` 可为单个物种 id 或列表。在**三类来源**搜索：背包(`/api/bag`，背包1/背包2)、仓库(`/api/storage`)、**精英背包**(`/api/exe`)。

```python
r = s.find_pet(5000)
# -> {"5000": {"locations": ["背包1(出战)", "仓库"], "count": 2}}
```

| 位置取值 | 说明 |
|---|---|
| `背包1(出战)` | 第一背包 |
| `背包2(待命)` | 第二背包 |
| `仓库` | 普通仓库(2303) |
| `精英背包` | 爱宠/精英仓库(2361) |

> `find_pet` 是**只读查询**（含精英背包），不会改动数据。

### 查找精灵的 catchTime：`find_pet_catchtime(ids)`
数据来源与 `find_pet` 完全一致（背包/仓库/精英背包），但把每只精灵的 **catchTime** 也带出来。
`find_pet` 只返回“位置”，而本函数返回该物种所有持有精灵的 **catchTime 列表**——适用于按 catchTime
定位的场景（如提交远征阵容 `42127`、按 catchTime 换宠等）。

```python
cts = s.find_pet_catchtime(5000)
# -> [1298723456, 99887766]           # 单个 id: 直接返回 catchTime 列表(同物种可多只)
cts = s.find_pet_catchtime([5000, 3512])
# -> {"5000": [...], "3512": [...]}    # 传入列表: 返回 {str(id): [catchTime,...]}
```

- **单个 id(int/str) -> 直接返回 `[catchTime, ...]` 列表**；传入列表 -> 返回 `{str(物种id): [catchTime, ...]}`。
- 某物种在三类来源均未持有 => 对应列表为空。
- 列表已**去重**（同一 catchTime 只留一次），顺序为 背包(出战/待命) -> 仓库 -> 精英背包。
- 只读查询；与 `find_pet` 一样依赖后端的背包/仓库/精英背包解析。

### 断线检测 / 断线重连（对脚本**透明**）
后端（webui）与本库共同做到"掉线自动暂停/恢复"，脚本一般**无需写任何断线检测**：
- **【被动】服务器/网络掉线**：后端监听检测到后，隔 `PASSIVE_RECONNECT_WAIT`(90s) **自动重连**；
  期间正在进行的 `send`/`recv`/`get_recv_value`/`Battle.*` 等请求会**自动阻塞等待**，后端恢复后
  **从断点继续**（库内对这类"连不上"的错误做透明重试）。脚本代码不用动。
- **【主动】我方中断**（如"主力阵亡 -> 立刻断线"避免判死）：脚本调 `drop_connection()` + `reconnect()`，
  后端**立即**重连。

```python
# 平时直接发命令即可; 被动掉线期间某条命令会被自动暂停, 等后端自愈后重新执行
pkt = s.recv(42126, [])        # 若此刻被动掉线, 会自动等 ~90s+重连 后成功返回

# 主动断线重连示例:
if s.is_connected():
    s.drop_connection()            # 我方主动断开(中止对局; 赶在 2506 提交前断线, 主力不判死)
s.reconnect(timeout=40)            # 立刻重新登录(若在线先断, 再重登)
```

- `is_connected() -> bool`：读 `/api/status` 的 `connected`。
- `drop_connection() -> dict`：`POST /api/disconnect`，我方主动断开（保留账号/凭据供重连）。
- `reconnect(timeout=30) -> dict`：**主动**重连——若在线先 `disconnect`（中止对局），再
  `POST /api/reconnect` 立刻重登，轮询 `/api/status` 至 `ready+connected`；超时/`error` 抛 `SeerError`。
- `wait_until_connected(timeout=120) -> dict`：可选——显式阻塞直到后端上线（配合后端被动自愈）；
  一般场景不需要（`send`/`recv` 已透明处理）。
- `/api/status` 暴露 `connected`、`disconnect_kind`（`server`/`active`）、
  `passive_reconnect_pending/wait/in`。

---

## 6. `Battle`：对战体（自动按回合驱动）

`Battle` 是**对局级**封装：以“带 `cmdid` 的完整 HEX 包”进入对战，随后**操作即回合**自动推进。

### 核心模型

- **进场自动完成**：`Battle(hex)` 构造时自动发送该包并**充分等待对战成功发起**（`active`+双方当前精灵，且该状态**连续稳定 `ENTRY_SETTLE=0.8s`** 无回退才返回）。无法正常进入（超时/收到结束包）抛 `SeerError`。若后端本就在对战则直接返回当前状态、不再重复发触发包。
- **操作即回合**：`use_skill`/`use_item`/`capture`/`escape` 发包后都会**自动等待本回合结算(2505)** 并返回，因此**不需要**写 `wait`/`wait_round`。
- **换宠例外**：`change_pet`（当前精灵阵亡时的**死亡切换**）**不消耗回合**——换上新精灵后可在**同一回合**继续出招；而对**还活着的精灵主动换宠**则**消耗一回合**（见 `change_pet` 的 `death` 参数）。
- **结束自动终止**：收到结束包(2506) 后 `finished` 置 `True`，循环自动退出。

### `Battle(hex_packet=None, base=None, timeout=30.0, probe=True, entry_timeout=15.0, heal=True)`
- `hex_packet`：带 `cmdid` 的完整 HEX 包（对战进入输入）。给则构造时自动 `start`。
- `entry_timeout`：进入对战/单次等待超时。
- `heal`（默认 `True`）：**每次真正发起新对战前**，先发 `47136 PET_CURE_FREE`（免费治疗背包，空包体）把出战/待命精灵 HP/PP 全满再开战；`False` 则不治疗。治疗应答超时/失败不会阻塞开战。

```python
from PySeer import Battle
battle = Battle("带cmdid的完整HEX包")     # 发送对战包 + 自动进场; 失败抛 SeerError
battle = Battle(hex, heal=False)         # 若不想要每次对战前治疗
while not battle.finished:
    my, other = battle.my, battle.other
    if my and (my.get('hp') or 0) <= 0:
        battle.change_pet(battle.my_team[1]['id'])   # 死亡切换(传物种id), 不消耗回合
        battle.use_skill(battle.skills[0])            # 同一回合内继续出招
    elif my and (my.get('hp') or 0) < 300:
        battle.use_item(300014)                       # 用道具(消耗一回合)
    else:
        battle.use_skill(battle.skills[0])            # 使用技能(消耗一回合)
    rnd = battle.round                                # 本回合(2505)数据
    print(rnd.get('first', {}).get('lostHP'))         # 本回合伤害
```

### 进场 / 推进（低级原语，通常无需手动调用）

| 方法 | 返回 | 说明 |
|---|---|---|
| `start(hex_packet=None, entry_timeout=None)` | `dict` | 发送触发包并等待进场；若已在对战中则直接返回 |
| `wait(timeout=8)` | `dict` / `None` | 阻塞到**下一个对战事件**（version 递增或结束）；超时返回 `None` |
| `wait_active(timeout=15)` | `dict` | 阻塞到进入对战（`active=True`）；超时抛 `SeerError` |
| `wait_round(timeout=15)` | `dict` | 阻塞到**一回合结果(2505)** 或结束，自动跳过非回合事件；超时抛 `SeerError` |

> `wait` 走后端 `/api/battle/wait` 长轮询（按 `version` 判断新事件）。这三个是底层原语；用 `use_skill` 等动作时它们已**自动完成**等待。

### 读取当前对战 / 回合数据（`Battle` 属性）

| 属性 | 类型 | 说明 |
|---|---|---|
| `state` | `dict` | 对战快照：`{active, finished, mode, my, other, myTeam, otherTeam, mySkills, lastCmd, lastSkill, report, ...}` |
| `active` | `bool` | 是否在对战中 |
| `finished` | `bool` | 是否已收到结束包(2506)，据此终止 |
| `my` | `dict` | **我方当前出战精灵**（结束后保留最后一帧） |
| `other` | `dict` | **敌方当前出战精灵**（同上） |
| `my_team` | `list[dict]` | 我方出战队伍 |
| `other_team` | `list[dict]` | 敌方出战队伍 |
| `skills` | `list[int]` | 我方当前可用技能 id 列表 |
| `round` | `dict` | **当前回合(2505)数据**：`first`(我方)/`second`(敌方) `AttackValue` + `hpUpdates`/`skillRecords`/`attackBlocks`/`endOffset` |
| `report` | `list` | 后端**精简战报**（`[{t,msg}]`）：只含 对战开始 / 每回合「在场精灵 使用技能 + 剩余HP」 / 对战结束结果 |
| `events` | `list` | 本对战体观察到的事件（`[{version, cmd, ts}]`） |
| `last_cmd` | `int` | 最近触及更新的命令号 |
| `version` | `int` | 已观察到的对战版本号 |
| `mode` | `int` | 对战模式(2503) |

**`my`/`other` 的关键字段**：`id`/`petID`（物种 id）、`petName`/`name`、`catchTime`、`hp`、`maxHP`/`maxHp`、`level`/`lv`、`skills`、`avatar`（头像 URL）。

**`round`（2505）结构**（详见 `seer/fightinfo.py::parse_note_use_skill`）：
- `first` / `second`：双方施法者的 `AttackValue`（`.userID`、`.skillID`、`.lostHP` 伤害、`.gainHP` 回血、`.remainHP`/`.maxHp` 结算后血量、`.isCrit`、`.status`、`.specailArr` 等）。
- `hpUpdates`：按 catchTime 扫到的全体精灵血量；`attackBlocks`/`skillRecords`：双方技能记录。
- `endOffset`：解到包体末尾的偏移。

### 操作（发包 / 用技能 / 换宠 / 用道具 / 捕捉 / 逃跑 / 战报）

| 方法 | 说明 |
|---|---|
| `send(cmd, params=None, encode="pack")` | 任意发包（命令名或命令号；`encode="hex"` 原样十六进制）；**不自动等回合** |
| `send_hex(hex_packet)` | 发送一条带 `cmdid` 的完整 HEX 包（后端重建 uid/序列号并加密封包） |
| `use_skill(skill_id)` | 用技能(2405)：发包后**自动等本回合结算(2505)**，消耗一回合 |
| `use_skill_smart(skill_id, *, pp_potion_id=300017, refill=True)` | **出招前检查该技能 PP**：耗尽时先用**中级活力药剂(300017)** 回复再出招；见下「智能出招」 |
| `use_item(item_id, catchTime=None)` | 用道具(2406)：包体 `[我方catchTime, 物品id, 0]`，`catchTime` 默认取当前出战精灵；发包后自动等本回合(2505)，消耗一回合 |
| `capture(*params)` | 捕捉(2409)：发包后自动等本回合(2505)，消耗一回合 |
| `change_pet(species_id, catchTime=None, *, death=None)` | **换宠**(2407)：`death=None` 自动判断——当前精灵阵亡→**死亡切换**(不消耗回合)；还活着→**主动切换**(消耗一回合)。`death=True/False` 可强制；见下 |
| `escape()` | 逃跑(2410)：发包后自动等对战结束(2506) |
| `skill_pp(skill_id)` | 取该技能**当前剩余 PP**（服务器 2505 经 `mySkillPP` 同步）；未同步返回 `-1` 表示未知 |
| `act(msg)` | 把一条脚本动作记入后端战报 |

> 所有会消耗回合的动作返回**本回合后的最新快照**（含 `round`/`my`/`other`）。终局回合会顺带等到结束包(2506)置 `finished`。

### `change_pet(species_id, catchTime=None, *, death=None, timeout=None) -> dict`

**换宠**：既可作为**死亡切换**（当前精灵阵亡时换上新精灵，**不消耗回合**，换完可继续出招），
也可作为**主动切换**（换掉还活着的精灵，**消耗一回合**）。通过 `death` 区分：

| `death` | 行为 |
|---|---|
| `None`（默认） | **自动判断**：当前我方出战精灵阵亡（`my.hp<=0` 或未知）→ 死亡切换；还活着 → 主动切换 |
| `True` | 强制**死亡切换**（不消耗回合，停在 `2407` 新精灵上场） |
| `False` | 强制**主动切换**（消耗一回合，换完后继续等本回合结算 `2505`，让调用方知道该回合已被换宠消耗） |

- **推荐传物种 id**：`battle.change_pet(5000)`。后端从**当前对战阵容**(`myTeam`)里查一只该 id 的可用精灵（排除当前出战的，优先存活），取其 `catchTime` 发包——**不用手填 catchTime**（那个值很难拿对）。
- 也可直传：`battle.change_pet(None, catchTime=目标catchTime)`。

两种情况都会发 `2407` + 目标精灵 catchTime，然后**等到 `my.catchTime` 发生变化**（新精灵真正上场）并把 `my`/`skills` 刷新为它。之所以等状态变化而不是 `lastCmd==2407`：2407 应答可能被紧随的 2505 覆盖 `lastCmd`，或对端(NPC)换宠(userID==0)不改我方 `my`，都会导致按 cmd 判别误判。

```python
# 死亡切换(精灵已阵亡): 不消耗回合, 换完同一回合继续出招
battle.change_pet(5000)                       # 按物种id
battle.use_skill(battle.skills[0])            # 死亡切换后同一回合继续出招

# 主动切换(精灵还活着): 消耗一回合, 换完后本回合结算, 下一回合再出招
battle.change_pet(5000, death=False)          # 或省略 death=None 自动按当前精灵存活判断
# 这回合已被这次换宠消耗; 下一个循环里再 use_skill 即是新的一回合
```

> 若阵容里找不到该 id（或就是当前出战/已阵亡），后端返回明确错误并 `SeerError`。
> `death` 默认自动判断通常够用；想强制某一种行为就显式传 `death=True` 或 `death=False`。

**用药（2406 USE_PET_ITEM）的包体是三个 int32**，不是只有物品 id：

```
[我方当前出战精灵 catchTime, 物品 id, 0]
```

依据客户端反编译 `refs/.../data/item/RenewBloodItemCategory.as`：

```actionscript
SocketConnection.send(CommandID.USE_PET_ITEM,
                      FighterModelFactory.playerMode.info.catchTime, itemID, 0);
```

> ⚠️ **只发物品 id 会被服务端判为非法操作** —— 实测立刻回 `2506 FIGHT_OVER` 并**断开游戏连接**。
> `use_item()` 已自动补 `catchTime`（取 `my.catchTime`），直接传物品 id 即可；
> 取不到 catchTime（未在对战 / 还没收到 2503·2504）会抛 `SeerError` 而**不会**发错包。

```python
battle.use_item(300014)                       # 超级体力药剂, catchTime 自动取
battle.use_item(300016, catchTime=12345678)   # 也可显式指定
```

### 智能出招：`use_skill_smart(skill_id, *, pp_potion_id=300017, refill=True, timeout=None) -> dict`

**出招前检查该技能 PP**，耗尽时自动用**中级活力药剂**回复后再出招。适用于"技能 PP 用尽就喝活力药剂补"的自动战斗。

```python
battle.use_skill_smart(10001)            # 用药回复 PP 后再自动出招
```

**判定逻辑**（照需求）：
1. 读该技能 **最大 PP**：取自资源表 `data/skills.json` 的 `pp`（模块级 `skill_max_pp(sid)`）。
2. 读该技能 **当前 PP**：`skill_pp(skill_id)`，由服务器每个 2505 的 `mySkillPP` 同步（权威剩余值）。
3. 若 `最大PP <= 0`（不限/未知），或 `当前PP > 0`/未知(`-1`) → **直接出招**。
4. 若 `最大PP > 0` 且 `当前PP == 0` → 回复流程：
   - `c = get_item_count(300017)`
   - **`c == 0`**（没货）：先 `buy_item(300017, 1)` 买一份 → `use_item` 喝掉 → **再出招**。
   - **`c > 0`**（有货）：先 `use_item` 喝掉 → 再 `buy_item(300017, 1)` 补回一份（仓库保持有货）→ **再出招**。

**参数**：
- `pp_potion_id`：回复技能 PP 的药剂 id，默认 `300017`（中级活力药剂）。
- `refill`：仓库有货时用完是否再补买一份（默认 `True`）。
- `timeout`：出招等待超时（秒），默认用 `entry_timeout`。

> 说明：
> - 出招走 `use_skill(2405)`；喝药走 `use_item(2406)`（消耗一回合，自动补 `catchTime`）；买药走 `buy_item(2601)`。
> - ⚠️ `buy_item`/`get_item_count` 走**非对战**通道；对战中能否直接买药取决于后端实现，若不可买可先在**对战外**囤好中级活力药剂（有货时默认走"先用再补买"分支）。
> - `skill_pp(skill_id) -> int`：未同步时返回 `-1`（**未知 ≠ 0**，避免误判为"PP 耗尽"）。

### `run(decide, timeout=15.0) -> bool`

**自动驱动整场对战直到结束包(2506)**。`decide(this)` 是每回合的决策回调：每回合对战体已更新状态（可直接读 `this.my`/`this.other`/`this.round`/`this.skills`），回调里决定并发出本回合动作。因为每个动作都会自动等回合，`run` 只需循环调 `decide` 直到 `finished` —— **你只写判断逻辑**。

```python
def decide(b):
    if b.my and (b.my.get('hp') or 0) <= 0:
        b.change_pet(b.my_team[1]['id'])
        b.use_skill(b.skills[0])
    else:
        b.use_skill(b.skills[0])
battle.run(decide)
```

> `decide` 每回合至少要发一个**消耗回合**的动作（use_skill/use_item/capture/escape），否则会空转。

---

## 7. 常用命令号速查

| cmd | 命令名 | 说明 |
|---|---|---|
| 2001 | ENTER_MAP | 进入地图 |
| 2301 | GET_PET_INFO | 单只精灵详情 |
| 2303 | GET_PET_LIST | 仓库列表 |
| 2304 | PET_RELEASE | 释放/取出精灵（存/取仓库，`[catchTime,pos]`） |
| 2308 | PET_DEFAULT | 设首发 |
| 2361 | GET_LOVE_PET_LIST | 精英(爱宠)背包 |
| 2404 | READY_TO_FIGHT | 准备就绪 |
| 2405 | USE_SKILL | 使用技能（body=技能id） |
| 2406 | USE_PET_ITEM | 用道具 |
| 2407 | CHANGE_PET | 换宠（body=目标 catchTime） |
| 2409 | CATCH_MONSTER | 捕捉 |
| 2410 | ESCAPE_FIGHT | 逃跑 |
| 2503 | NOTE_READY_TO_FIGHT | 出场队伍(NOTE) |
| 2504 | NOTE_START_FIGHT | 开场双方当前精灵 |
| 2505 | NOTE_USE_SKILL | 回合结果(NOTE) |
| 2506 | FIGHT_OVER | 对战结束 |
| 41462 | (交换/移动精灵) | 背包内换位 |
| 43706 | GET_PET_INFO_BY_ONCE | 整批查背包精灵 |
| 41921 | | 阵容列表 |
| 41922 | | 切换阵容 |

> 命令名完整列表见 `app/cmdmap.json`（`id -> name`）。

---

## 8. 完整示例

### 示例一：查背包 + 取值 + 换背包
```python
from PySeer import Seer
s = Seer()
# 刷新背包
s.send(43706)
pkt = s.recv(43706)
print("首只精灵 id:", pkt[0])
# 查某物种在哪
r = s.find_pet([5000, 3512])
print(r)
# 把背包切成指定阵容
s.set_bag([5000, 5001, 5002])
```

### 示例二：自动对战（`run`）
```python
from PySeer import Battle

BATTLE_HEX = "00000015310000A0A9383934A3000002B700001A48"   # 换成你的真实触发包

def decide(b):
    my = b.my or {}
    if (my.get('hp') or 0) <= 0:                    # 阵亡 -> 死亡切换(不耗回合)后出招
        reserve = next((p for p in b.my_team
                        if p.get('catchTime') != my.get('catchTime') and (p.get('hp') or 0) > 0), None)
        if reserve:
            b.change_pet(reserve['id'])
            b.use_skill(b.skills[0])
        else:
            b.escape()
    elif (my.get('hp') or 0) < 300:                 # 残血 -> 用药(耗一回合)
        b.act("> 血量偏低, 用药")
        b.use_item(300014)                          # 超级体力药剂(catchTime 自动补)
    else:
        b.use_skill(b.skills[0])                    # 正常出招(耗一回合)

battle = Battle(BATTLE_HEX)                         # 自动进场
battle.run(decide)                                  # 自动打到结束
print("对战结束:", battle.finished, "回合数可见 report", battle.report[-3:])
```

### 示例三：底层按回合读数据
```python
from PySeer import Battle
b = Battle(BATTLE_HEX)
while not b.finished:
    b.use_skill(b.skills[0])
    rnd = b.round                        # 本回合数据
    me = rnd.get('first') or {}
    foe = rnd.get('second') or {}
    print(f"我方技能{me.get('skillID')} 伤害{me.get('lostHP')} 剩余HP {me.get('remainHP')}/{me.get('maxHp')}")
    print(f"敌方技能{foe.get('skillID')} 伤害{foe.get('lostHP')} 剩余HP {foe.get('remainHP')}/{foe.get('maxHp')}")
```

### 示例四：技能 PP 用尽自动喝活力药剂再出招
```python
from PySeer import Battle
b = Battle(BATTLE_HEX)
while not b.finished:
    # 用完自动判断: PP 耗尽就用中级活力药剂(300017)回复后再出招, 否则直接出招
    b.use_skill_smart(b.skills[0])
print("对战结束:", b.finished)
```

> 若想**只出招不补货**：`b.use_skill_smart(b.skills[0], refill=False)`。

---

## 9. 注意事项与边界

1. **必须先登录后端**：`Seer`/`Battle` 的相关操作都要求后端 `webui.py` 已启动并登录游戏账号。
2. **`set_bag` 会发真实游戏命令**（2304/2308/41462），会物理重排背包；请用安全/可恢复的列表。
3. **对战需要真实触发包**：`Battle(hex)` 的 `hex` 是带 `cmdid` 的完整封包（从游戏抓包或已记录包得到）；包无效则 `_wait_entry` 无法等到进场，超时抛 `SeerError`。
4. **进场充分等待**：`_wait_entry` 会等 `active`+双方当前精灵**连续稳定约 0.8s** 才返回；上一场遗留的 `finished=True` 会被忽略，避免“二次运行即报错”。
5. **`use_item` 会自动补 `catchTime`**：2406 包体是 `[catchTime, 物品id, 0]`（见上文「用药」一节），只发物品 id 会导致服务端回 2506 并**断线**。`capture`/`escape` 仍按原样发包（`capture` 需自行传胶囊物品 id；若要完全自定义包体请用通用 `send(cmd, params)`）。
6. **换宠按 id**：后端从当前对战阵容解析 `catchTime`；若该 id 不在阵容（或就是当前出战/已阵亡），会返回“阵容中找不到”错误。
7. **回合是“操作即回合”**：每发一个消耗回合的动作会**自动等待 2505**；只有 `change_pet`（死亡切换）不消耗回合，可在同一回合再出招。
8. **异常统一 `SeerError`**：参数错/超时/未登录/越界都会抛 `SeerError`，捕获后处理即可。
9. **运行时数据**：`_pet_name()` 等依赖 `data/petbook.json`（自更新）；`find_pet`/`set_bag` 依赖后端的背包/仓库/精英背包解析。
10. **`use_skill_smart` 回复 PP 依赖活力药剂**：出招前检查技能 PP，耗尽时用中级活力药剂(300017)回复。其中 `buy_item`/`get_item_count` 走**非对战**通道（`/api/send-recv`）；对战中若不能直接买药，请先在**对战外**囤好活力药剂（有货时默认走"先用再补买"分支），或设 `refill=False` 只喝不补。

---

## 附：模块级函数/常量

| 名 | 说明 |
|---|---|
| `get_value(body, index)` | 从包体取第 `index` 个 int32 |
| `discover_backend(...)` | 自动定位后端地址 |
| `skill_max_pp(sid)` | 按技能 id 查**最大 PP**（`data/skills.json` 的 `pp`）；查不到返回 `0` |
| `PP_RESTORE_ITEM` | 中级活力药剂物品 id `300017`（恢复技能 PP）；`use_skill_smart` 默认用它 |
| `PET_CURE_FREE` | 免费治疗背包(HP/PP 全满)命令 `47136`（空包体）；`Battle(heal=True)` 每次对战前自动发它 |
| `parse_map_player_list(body)` | 解析 `2003 LIST_MAP_PLAYER` 应答 → `[{userID,nick,pos,fireBuff,…}]`（`UserInfo.setForPeoleInfo`） |
| `FIRE_TYPES` | `{0:无火, 5:绿火}`（已确认 5=绿火；蓝/紫/金未实测） |
| `DEFAULT_BORROW_FIRE` | 借火默认目标值 `5`（绿火，地图最常见） |
| `Packet` | RECV 包体对象 |
| `SeerError` | 库调用异常 |
| `DEFAULT_BASE` | 兜底后端地址 `http://127.0.0.1:8680` |
| `Battle.ENTRY_SETTLE` | 进场稳定性窗口（秒，默认 0.8） |

> 自检：`PYTHONPATH=app python3 -m app.PySeer`（需后端已登录；会刷背包并打印 43706 包体）。

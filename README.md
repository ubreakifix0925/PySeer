# PySeer — 可脱机的赛尔号后端 · WebUI 控制台 · 第三方库

> **PySeer**是一个**纯标准库**实现的赛尔号工具套件，专注三件事：
> **①** 一个**可脱机运行**的赛尔号协议后端；**②** 一个用于调试/观察的 **WebUI 控制台**；
> **③** 一个**高度可扩展**的第三方库 **`PySeer`**，供脚本按命令级驱动游戏。
>
> 它复刻了赛尔号客户端（Flash/H5）的登录与通信流程（淘米认证 → 网关握手 → WebSocket/裸 TCP 加密登录 →
> 会话密钥派生 → 心跳保活），用于协议学习、验证与脚本开发。



---

## ⚡ 快速开始（一键部署 + 启动）

### A. 未下载项目？一行脚本全自动安装并启动（从零机器）

只要机器有 Python（或能自动装 Python），一条命令即完成：**下载项目 → 装 Python → 启动 WebUI**：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ubreakifix0925/PySeer/main/deploy.sh)
# 换端口:  ... deploy.sh) 9000
# 监听所有网卡:  PYSEER_HOST=0.0.0.0 bash <(curl -fsSL <同上>) 
```

> `deploy.sh` 会：`①` 校验/安装 `python3`（缺失时按 `apt/brew/dnf/pacman` 自动装）；`②` `git clone`
> （或下载源码包）到 `./PySeer`；`③` 执行 `./start.sh` 启动控制台。等价于：
> ```bash
> git clone https://github.com/ubreakifix0925/PySeer.git PySeer && cd PySeer && ./start.sh
> ```
> 环境变量 `PYSEER_REPO` / `PYSEER_DIR` / `PYSEER_HOST` / `PYSEER_PORT` / `PYSEER_NO_UPDATE=1` 可覆盖默认。

### B. 已下载项目（本地一键启动）

```bash
./start.sh                          # 默认 http://127.0.0.1:8680/
./start.sh 9000                     # 换端口
./start.sh --update                 # 启动前先刷新一次游戏数据(精灵名/属性/技能/魂印/头像)
PYSEER_HOST=0.0.0.0 ./start.sh      # 监听所有网卡
```

### C. 不依赖脚本的一条命令

```bash
python3 -u app/webui.py --port 8680 --no-update
# 需要游戏数据自更新时(需 vendor/unitypy):
PYTHONPATH=vendor/unitypy python3 -u app/webui.py --port 8680
```

> 浏览器打开 **http://127.0.0.1:8680/** ，在「登录」页填米米号 + 密码即可。
> `start.sh`/`deploy.sh` 会在缺失 `vendor/unitypy` 时自动加 `--no-update`，保证**无需任何第三方依赖也能一键启动**。

---

## 目录

- [⚡ 快速开始（一键部署 + 启动）](#快速开始一键部署--启动)
- [项目定位](#项目定位)
- [特性](#特性)
- [目录结构](#目录结构)
- [部署](#部署)
- [使用](#使用)
- [文档索引](#文档索引)
- [合规与免责](#合规与免责)

---

## 项目定位

PySeer 由三部分协同组成：

1. **可脱机的赛尔号后端**（`app/seer/`）
   纯标准库（`stdlib`）实现的协议客户端：淘米认证、网关解析、WebSocket/裸 TCP 连接、封包加解密
   （位移 + XOR，密钥 `!crAckmE4nOthIng:-)`）、登录后**会话密钥派生**（参考帖规则）、心跳保活。
   **核心价值**：不依赖第三方库即可在本地复现整条登录与通信链路，便于逆向学习与二次开发。

2. **WebUI 控制台**（`app/webui.py`）
   一个 `http.server` + SSE 的调试控制台，提供：登录操作、每个收发封包实时日志、
   **精灵详情（属性/能力值/专属特性/技能）**、**背包/仓库拖拽管理**、**脚本运行器**（内置脚本目录），
   以及 **图形化对战页**（自动检测到对战即打开，实时显示双方精灵/技能/回合/战报）。
   同时内置**自更新游戏数据管线**（`assets_updater.py`），从游戏资源自动导出精灵名/属性/技能/魂印等。

3. **第三方库 `PySeer`**（`app/PySeer.py`）
   面向脚本开发者提供的高扩展库（仅 `urllib`），通过 HTTP 调用已登录的后端。分两个层次：
   - **命令级 `Seer`**：发/收/取包、查/改背包、查物品数量（`send`/`recv`/`get_value`/`get_recv_value`
     /`set_bag`/`find_pet`/`get_item_count`）。
   - **对局级 `Battle`**（对战体）：以"带 `cmdid` 的完整 HEX 包"进入对战，**自动按回合推进**（操作即回合、
     死亡切换不消耗回合、进场自动完成），可读取当前回合数据并用任意复杂判断驱动决策。

> 旧名 **`seerlib`** 仍保留为兼容别名（`app/seerlib.py` 原样转出 `PySeer`），`from seerlib import ...`
> 依旧可用；新代码请统一 `import PySeer`。

---

## 特性

- **零第三方依赖**：后端与脚本库均只用 Python 标准库（`urllib`/`http.server`/`socket`/`ssl`）。
- **可脱机**：整条登录链路 + 封包结构 + 算法在本地复刻，不依赖他人解析表；游戏数据也由内置管线**自更新**。
- **WebUI 实时控制台**：SSE 实时日志、收发包过滤、命令名映射（`cmdmap.json`，约 2910 条）、精灵详情/专属特性/技能、
  背包与仓库拖拽、脚本运行器、图形化对战页。
- **高扩展第三方库**：`Seer`（命令级）与 `Battle`（对局级）分层；`Battle` 自动处理回合/进场/换宠，
  脚本只需写判断逻辑。
- **多端可跑**：从项目根或 `app/` 目录均可运行，只需 `PYTHONPATH` 含 `vendor/unitypy`（用于数据自更新）。

---

## 目录结构

```
PySeer/
├── app/                     # 程序文件 (运行必需)
│   ├── webui.py             # WebUI 控制台 (http://127.0.0.1:8680): 登录/日志/精灵详情/脚本/对战页
│   ├── PySeer.py            # 第三方库: Seer(命令级) + Battle(对战体)
│   ├── seerlib.py           # PySeer 的旧名兼容别名 (from seerlib import ... 仍可用)
│   ├── assets_updater.py    # 自更新游戏数据管线 (精灵名/属性/技能/魂印/头像)
│   ├── login_test.py        # 登录协议自检/测试入口
│   ├── mock_server.py       # 模拟网关 (不联网自检用)
│   ├── cmdmap.json          # 命令 id -> 命令名 (约 2910 条)
│   ├── seer/                # 协议客户端包 (可脱机后端核心)
│   │   ├── algorithm.py     # MD5 / Encrypt / Decrypt / MSerial
│   │   ├── body.py          # pack_body / decode_body / parse_parts
│   │   ├── packet.py        # PacketData 构建/解析 + 包体加解密
│   │   ├── session.py       # 淘米认证 -> session
│   │   ├── client.py        # SeerClient: 连接/登录/心跳/会话密钥派生
│   │   ├── ws_client.py     # 标准库最小 WebSocket 客户端
│   │   ├── tcp_client.py    # 游戏服务器裸 TCP 加密客户端
│   │   ├── petinfo.py       # PetInfo 各段解析 (依据反编译 *.as)
│   │   ├── fightinfo.py     # 对战包解析 (2503/2504/2505/2506/2407/...)
│   │   └── misc.py
│   └── scripts/             # "脚本"页默认脚本目录 (用户把 .py 放进来即可在页面运行)
├── data/                    # 运行时下载/生成的资源 (git 忽略)
├── refs/                    # 逆向参考资料 (git 忽略)
├── analysis/                # 抓包分析工具与产物 (git 忽略)
├── docs/                    # 文档 (开发成果/协议复现/PySeer API)
├── cache/  vendor/  webui_logs/   # 运行时缓存/用具/日志 (git 忽略)
├── start.sh                # 本地一键启动 WebUI 控制台
├── deploy.sh               # 从零一键部署(下载项目+装 Python+启动) — 未下载项目时用
└── README.md               # 本文档
```

---

## 部署

### 环境要求

- **Python ≥ 3.8**，无第三方依赖。
- （可选）数据自更新需要 `vendor/unitypy`（若缺失，`assets_updater` 会自动安装到 `vendor/`，不污染系统）。
- 建议在项目根目录执行命令（后端/脚本库会通过 `__file__` 自动定位项目根与 `data/`）。

### 1) 启动后端 / WebUI 控制台

**一键脚本（推荐）**：

```bash
./start.sh                     # 直接启动到 http://127.0.0.1:8680/
```

**或手动一条命令**（项目根目录；`PYTHONPATH` 指向 `vendor/unitypy` 用于数据自更新）：

```bash
PYTHONPATH=vendor/unitypy nohup python3 -u app/webui.py --port 8680 >/tmp/pyseer_webui.log 2>&1 &
```

启动后浏览器打开 **http://127.0.0.1:8680/** 。在「登录」页填米米号 + 密码并登录（默认连
游戏服务器 `101.43.19.60:1201`）。登录成功后自动派生会话密钥并开启后台监听。
`start.sh` 在缺失 `vendor/unitypy` 时会自动加 `--no-update`，无需任何第三方依赖即可启动。

常用参数（`python3 app/webui.py --help` 查看全部）：

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址（默认 `127.0.0.1`） |
| `--port` | 监听端口（默认 `8680`；`--port 0` 自动选空闲端口，实际端口写入 `data/webui_addr.json`） |
| `--no-update` | 启动时不检查/更新本地精灵头像（默认会自动更新） |
| `--update-force` | 强制重下载并解包精灵头像 |

> 📌 后端会把**实际监听地址**写入 `data/webui_addr.json`，脚本库 `PySeer` 运行时据此自动定位后端。

### 2) 自更新游戏数据（可选）

```bash
PYTHONPATH=vendor/unitypy python3 app/assets_updater.py --force
```

产出到 `data/`：`petbook.json`（精灵名）、`pet_attr.json`（属性）、`skills.json`（技能）、
`soulmarks.json`（专属特性/魂印）、`head/*.png`（头像）、`effecticon/*.png`（效果图标）。
每份数据带版本状态文件，命中版本即跳过。

### 3) 登录协议自检 / 干跑（不联网）

```bash
# 离线自检: 验证算法/封包/加解密/JSONP 解析
python3 app/login_test.py --self-test
# 干跑: 不访问服务器, 仅本地构建登录封包
python3 app/login_test.py --account 1234567890 --password 你的密码 --dry-run
```

---

## 使用

### 1. WebUI 控制台

`webui.py` 的页面分四大块：

- **登录**：填米米号/密码/游戏服IP/端口一键登录（或提供 `session` 跳过淘米认证）。
- **日志 / 响应表**：SSE 实时推送每个收发封包（含命令名、命令号、包体、十进制数组）；可按"过滤包id"
  与"收发开关"筛掉噪声包。
- **精灵/背包**：精灵详情（属性/能力值/专属特性/技能）、背包与仓库拖拽、切换阵容、查询背包精灵(43706)。
- **脚本**：列出 `app/scripts/` 下的 `.py` 脚本，一键后台运行，`print` 实时输出到"脚本输出"控制台。
- **对战**：**后台一旦监听到对战行为即自动切到对战页**——图形化显示我方/敌方当前出战精灵（头像/血条/等级）、
  双方出场队伍、技能按钮（点击发 `2405 USE_SKILL`）、换宠/用药/捕捉/逃跑；并可手动粘贴"带 cmdid 的完整 HEX 包"
  发起任意对战。战报已**精简**为：对战开始 / 每回合在场精灵"使用技能+剩余HP" / 对战结果。

### 2. 第三方库 `PySeer`

脚本库通过 HTTP 调用已登录的后端，脚本只需 `import PySeer`（自动定位后端，无需硬编码地址）。

#### 快速开始 — 命令级 `Seer`

```python
from PySeer import Seer
s = Seer()                                  # 自动定位后端
s.send(43706)                              # 发包(不等待响应)
pkt = s.recv(2301, [3266, 0, 0, 0])        # 发包并等该命令 RECV, 返回 Packet
print(pkt.ints, s.get_value(pkt, 0))       # 取应答第 0 个 int32
n = s.get_item_count(2600048)              # 物品数量 (发 42399, 取应答第 3 个参数)
r = s.find_pet(5000)                       # 查某物种在哪
s.set_bag([5000, 5001, 5002])              # 物理重排背包为指定阵容
```

#### 快速开始 — 对局级 `Battle`

```python
from PySeer import Battle
battle = Battle("带cmdid的完整HEX包")     # 发送对战包 + 自动进场 (失败抛 SeerError)
while not battle.finished:
    my = battle.my or {}
    if (my.get('hp') or 0) <= 0:                       # 阵亡 -> 死亡切换(不耗回合)后出招
        battle.change_pet(battle.my_team[1]['id'])
        battle.use_skill(battle.skills[0])
    elif (my.get('hp') or 0) < 300:
        battle.use_item(70001)                         # 用道具(耗一回合)
    else:
        battle.use_skill(battle.skills[0])             # 用技能(耗一回合)
    rnd = battle.round
    print(rnd.get('first', {}).get('lostHP'))
```

也可用 `battle.run(decide)` 自动驱动整场直到结束包：

```python
def decide(b):
    if b.my and (b.my.get('hp') or 0) <= 0:
        b.change_pet(b.my_team[1]['id']); b.use_skill(b.skills[0])
    else:
        b.use_skill(b.skills[0])
battle.run(decide)
```

#### 主要 API 一览

| 层次 | 类/函数 | 说明 |
|---|---|---|
| 命令级 | `Seer().send(cmd, params)` | 发包（不等待响应） |
| 命令级 | `Seer().recv(cmd, params, timeout=8)` | 发包并等该命令 RECV，返回 `Packet` |
| 命令级 | `Seer().get_value(body, index)` | 从包体取第 `index` 个 int32 |
| 命令级 | `Seer().get_recv_value(cmd, params, index)` | 发包→等 RECV→取应答第 `index` 个值 |
| 命令级 | `Seer().get_item_count(item_id)` | 获取物品数量（发 42399，取应答第 3 个参数） |
| 命令级 | `Seer().set_bag(ids)` | 物理重排背包为指定物种 id 列表（发真实命令） |
| 命令级 | `Seer().find_pet(ids)` | 查找物种在背包/仓库/精英背包的位置 |
| 对局级 | `Battle(hex)` / `Battle().start(hex)` | 发送 HEX 包进入对战并自动进场 |
| 对局级 | `Battle().use_skill(id)` / `use_item(...)` / `capture(...)` | 各操作，发包后自动等本回合结算(2505) |
| 对局级 | `Battle().change_pet(id)` | 换宠（默认自动判断死亡切换/主动切换） |
| 对局级 | `Battle().escape()` | 逃跑并等对战结束(2506) |
| 对局级 | `Battle().run(decide)` | 自动驱动整场直到结束包(2506) |
| 对局级 | `battle.my/other/round/skills/report/...` | 读取当前对战/回合数据（详见 `docs/PySeer.md`） |
| 公共 | `SeerError` | 库调用异常（未登录/参数错/超时/越界等） |
| 公共 | `discover_backend()` | 自动定位后端地址 |

> 📘 `PySeer` 的**完整 API / 参数 / 返回 / 示例 / 注意事项**见专项文档 [`docs/PySeer.md`](./docs/PySeer.md)。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) | 开发成果整理：自更新数据管线、精灵详情功能、协议逆向结论、`PySeer` 脚本库、待办 |
| [`docs/REPRODUCTION.md`](./docs/REPRODUCTION.md) | 给 AI / 二次开发者的**协议技术复现**速查（登录/加解密/封包/精灵/对战解析） |
| [`docs/PySeer.md`](./docs/PySeer.md) | **第三方库 `PySeer` 的完整 API / 用法** |

---

## 合规与免责

1. 本工具仅用于账号做登录协议测试与学习；请勿用于盗号、凭证窃取或违反服务条款。
2. 明文密码仅在本地计算 MD5 后发送，不会以明文落盘；仍请在受控环境运行，勿在他人机器上使用。
3. 协议/密钥可能随版本更新：若真实登录失败，参考 `refs/` 与 `docs/REPRODUCTION.md` 对齐最新参数。
4. 游戏自动化有账号冻结风险，请自行评估并遵守淘米/赛尔号服务条款。

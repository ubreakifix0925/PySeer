# PySeer

纯标准库实现的赛尔号工具套件：可脱机的**协议后端** + **WebUI 控制台** + 第三方库 **`PySeer`**。

> ⚠️ 仅用于对**你自己拥有**的账号做协议学习与验证；请勿用于批量登录、盗号、凭证窃取或违反游戏服务条款。

## 项目组成

| 部分 | 位置 | 作用 |
|---|---|---|
| 后端 | `app/seer/` | 登录、收发包、加解密、心跳（纯标准库） |
| WebUI 控制台 | `app/webui.py` | 登录、实时封包日志、精灵详情/背包/脚本、图形化对战 |
| 第三方库 | `app/PySeer.py` | `Seer`（命令级）+ `Battle`（对战体），供脚本调用后端 |

## 快速开始

前置：Python 3.8+。

### 未下载项目

**Linux / macOS**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ubreakifix0925/PySeer/main/deploy.sh)
```

**Windows（PowerShell，需 Git）**

```powershell
git clone https://github.com/ubreakifix0925/PySeer.git
cd PySeer
python app\webui.py --port 8680
```

### 已下载项目

**Linux / macOS**

```bash
./start.sh
```

**Windows（PowerShell）**

```powershell
python app\webui.py --port 8680
```

**Windows（Git Bash / WSL）**

```bash
bash start.sh
```

启动后打开 `http://127.0.0.1:8680/` 并登录。

## 使用

### WebUI 控制台

登录 → 实时日志/响应表 → 精灵详情/背包管理 → 脚本运行（脚本放本地 `app/scripts/`，不入库）→ 对战页（后台检测到对战即自动打开）。

### 第三方库 PySeer

```python
from PySeer import Seer
s = Seer()                      # 自动定位已登录后端

pkt = s.recv(2301, [3266, 0, 0, 0])   # 发包并等 RECV, 返回 Packet
s.get_value(pkt, 0)                   # 取应答第 0 个 int32
s.get_item_count(2600048)             # 物品数量 (发 42399, 取应答第 3 个参数)
s.set_bag([5000, 5001, 5002])         # 物理重排背包为指定阵容
```

```python
from PySeer import Battle
battle = Battle("带cmdid的完整HEX包")  # 自动进场, 失败抛 SeerError
while not battle.finished:
    battle.use_skill(battle.skills[0])
    print(battle.round)               # 本回合(2505)数据
```

常用 API：`send` / `recv` / `get_value` / `get_recv_value` / `get_item_count` / `set_bag` / `find_pet` / `Battle`（`use_skill` / `use_item` / `capture` / `change_pet` / `escape` / `run`）。完整说明见 [`docs/PySeer.md`](./docs/PySeer.md)。

## 目录结构

```
app/                    # 程序文件
├── webui.py            # WebUI 控制台
├── PySeer.py           # 第三方库 (Seer/Battle)
├── seerlib.py          # PySeer 旧名兼容别名
├── assets_updater.py   # 自更新游戏数据管线
├── login_test.py       # 登录协议自检
├── mock_server.py      # 模拟网关
├── cmdmap.json         # 命令 id -> 命令名
├── requirements.txt
└── seer/               # 协议客户端包 (后端)
    ├── algorithm.py    # MD5/加解密/序列号
    ├── body.py         # 包体打包/拆分
    ├── packet.py       # 封包构建/解析
    ├── session.py      # 淘米认证
    ├── client.py       # SeerClient 连接/登录/心跳
    ├── ws_client.py    # WebSocket 客户端
    ├── tcp_client.py   # 游戏服务器裸 TCP 客户端
    ├── petinfo.py      # 精灵信息解析
    ├── fightinfo.py    # 对战包解析
    └── misc.py
docs/                   # 文档
├── PySeer.md           # 第三方库 API
├── REPRODUCTION.md     # 协议复现
└── DEVELOPMENT.md     # 开发成果
start.sh                # 本地一键启动
deploy.sh               # 从零一键部署
```

## 文档

- [`docs/PySeer.md`](./docs/PySeer.md) — 第三方库 `PySeer` 完整 API
- [`docs/REPRODUCTION.md`](./docs/REPRODUCTION.md) — 协议技术复现
- [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) — 开发成果整理

## 合规

仅用于自己账号的协议学习与验证；协议/密钥可能随版本更新，失败时参考 `docs/REPRODUCTION.md`；游戏自动化有账号冻结风险。

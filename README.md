# PySeer

纯标准库实现的赛尔号工具套件：可脱机的**协议后端** + **WebUI 控制台** + 第三方库 **`PySeer`**。


## 项目组成

| 部分 | 位置 | 作用 |
|---|---|---|
| 后端 | `app/seer/` | 登录、收发包、加解密、心跳（纯标准库） |
| WebUI 控制台 | `app/webui.py` | 登录、实时封包日志、精灵详情/背包/脚本、图形化对战 |
| 第三方库 | `app/PySeer.py` | `Seer`（命令级）+ `Battle`（对战体），供脚本调用后端 |

## 快速开始（跨平台）

前置：**Python 3.8+**、Git（推荐）。

### 1. 准备环境

| 系统 | 安装 Python + Git |
|---|---|
| Windows | 装 Python 时勾选 **Add Python to PATH**；再装 [Git for Windows](https://git-scm.com/) |
| macOS | `brew install python git`（或官网 pkg） |
| Debian / Ubuntu | `sudo apt-get update && sudo apt-get install -y python3 python3-pip git` |

装好后确认：`python3 --version`（Windows 可用 `python --version` / `py --version`）。

### 2. 拉取代码（一次性）

```bash
git clone https://github.com/ubreakifix0925/PySeer.git
cd PySeer
```

（没有 Git 就从 GitHub 页面下载 zip 解压后进入该目录。）

### 3. 启动控制台（在项目根目录）

| 系统 | 命令 |
|---|---|
| Linux / macOS | `python3 -u app/webui.py --port 8680` |
| Windows（PowerShell / cmd） | `python -u app\webui.py --port 8680` |
| Windows（Git Bash / WSL） | `python3 -u app/webui.py --port 8680` |

- **仓库已内置一份数据基线**（`data/petbook.json`、`pet_attr.json`、`skills.json`、`soulmarks.json`），`git clone` 后**离线即可显示**精灵名/属性/技能/魂印。
- 启动时会自动检查并保持数据为最新（需联网；缺 `UnityPy` 会自动按**当前平台**装到 `vendor/`——`UnityPy` 含平台相关编译产物，故不入 git 仓库，仅在刷新数据时按需自动安装）。头像/图标（`data/head`、`data/effecticon`）也在启动时按需下载。
- `python` 起不来时，Windows 换 `py -u app\webui.py --port 8680`。
- 换端口 `--port <端口>`；被占用会自动选空闲端口并打印实际地址；局域网访问加 `--host 0.0.0.0`。

### 4. 打开控制台

浏览器访问 **http://127.0.0.1:8680/** ，填米米号/密码登录，即可看到实时封包日志、精灵详情/背包管理、脚本运行与图形化对战。

---

> 可选：`--no-update` 可跳过启动时的数据刷新（更快，但游戏数据若已更新会偏旧）。
> 习惯 Bash 的可用一行脚本：本地启动 `./start.sh`；未下载项目时一键“装依赖+下载+启动”
> `bash <(curl -fsSL https://raw.githubusercontent.com/ubreakifix0925/PySeer/main/deploy.sh)`。

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

## 更新本项目

### 更新代码（工具本身）

- **git 克隆方式**：在项目目录执行
  ```bash
  git pull
  ```
  因为 `data/`（游戏数据）、`vendor/`（UnityPy）、`cache/`（下载缓存）都被 git 忽略，
  `git pull` 只会更新程序文件（`app/`、`docs/`、`README.md` 等），**不会动你本地的游戏数据/依赖**。
  若提示本地有改动冲突，先 `git stash` 或丢弃 `app/` 里的本地改动再 `git pull`。

- **zip 方式（无 Git）**：重新下载 zip，只替换 `app/`、`docs/`、`README.md`、`start.sh`、`deploy.sh`；
  `data/`、`vendor/`、`cache/` 原样保留（避免重下游戏数据与 UnityPy）。

### 更新游戏数据（精灵名/属性/技能/魂印/头像）

游戏资源**随时更新**，工具**启动时会自动检查并按版本增量刷新**数据。想立即全量刷新：

```bash
python3 -u app/assets_updater.py --force     # Linux / macOS（Windows 用 python / py）
# 或
./start.sh --update
```

### 更新后重启

```bash
# Linux / macOS
python3 -u app/webui.py --port 8680
# Windows
python -u app\webui.py --port 8680
```

> 若 `git pull` 后启动报错或数据异常，多半是本地 `data/`/`vendor/`/`cache/` 与新版本不匹配——
> 删除这三个目录后重跑一次 `python3 app/assets_updater.py --force`（它们都会自动重新生成/重下），再启动即可。

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

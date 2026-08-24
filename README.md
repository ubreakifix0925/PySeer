# 赛尔号 (Seer) 登录测试

一个 **纯标准库** 实现的赛尔号脱机登录协议测试工具。它复刻了赛尔号 Flash/H5 客户端
的登录流程（淘米帐号认证 → 网关握手 → WebSocket 登录封包 → 心跳保活），可以分步骤
验证整条登录链路是否走通，适合逆向学习与协议验证。

> ⚠️ **使用边界**：本工具仅用于对 **你自己拥有** 的赛尔号账号做登录协议测试与学习。
> 请勿用于批量登录、账号盗取、凭证窃取（中间人攻击）或任何违反游戏服务条款的行为。
> 本文参考的两篇 52pojie 帖文中，`thread-1468888` 是关于**通信协议逆向与模拟**的，
> 其中“中间人攻击窃取登录凭证”的部分**不在此实现范围内**。

> 📘 **相关文档**：自更新游戏数据管线、精灵详情界面功能、协议逆向结论、脚本库 `seerlib.py`
> 等成果详见 [DEVELOPMENT.md](./DEVELOPMENT.md)。

---

## 参考来源

- [《[原创] 赛尔号：通信协议逆向与模拟&中间人攻击窃取登录凭证》](https://www.52pojie.cn/thread-1468888-1-1.html) —— 协议逆向来源
- [《[原创] 赛尔号逆向：封包捕获与分析》](https://www.52pojie.cn/thread-2053139-1-1.html) —— 封包捕获/抓包思路
- 公开实现：[`Altriazyk/seerNew`](https://github.com/Altriazyk/seerNew)（脱机登录参考实现）、[`iyzyi/SeerPacket`](https://github.com/iyzyi/SeerPacket)、[`Starlitnightly/seer_py`](https://github.com/Starlitnightly/seer_py)

下载到本地的参考页与源码位于 `refs/` 目录，便于比对。

---

## 登陆协议概览

```
账号 → 淘米认证 (account-co.61.com)         得到 session
                 │
                 ▼
网关解析 (seerh5login.61.com/online_gate)   得到 ws://host:port
                 │
                 ▼
WebSocket 连接 ──► 发送登录封包 (cmd 0x3E9 = 1001)
                 │
                 ◄── 服务器登录应答 (cmd 1001, 携带序列号)
                 │
                 ▼
心跳保活 (cmd 1002) + 时间校验应答
```

封包结构（编解码时以十六进制字符串表示，传输时是裸字节）：

```
+--------+------+--------+--------+--------+----------+
| length |  ver | cmdId  | userId | result |   body   |
| 4B     | 1B   | 4B     | 4B     | 4B     |   ...    |
+--------+------+--------+--------+--------+----------+
```

关键算法：

- **MD5**：淘米认证中 `passwd` 字段 = 明文密码的 MD5。
- **协议体加解密（`Encrypt`/`Decrypt`）**：`位移 + XOR` 的简单对称算法，密钥
  `!crAckmE4nOthIng:-)`，详见 `seer/algorithm.py`。
- **序列号（`MSerial`）**：用包体异或值 + 包体长度 + 命令号生成合法性序列号。

---

## 目录结构

```
seer-login-test/
├── login_test.py          # 入口：分步骤 PASS/FAIL 的运行器
├── seer/                  # 可复用的协议包
│   ├── algorithm.py       # MD5 / Encrypt / Decrypt / MSerial
│   ├── misc.py            # hex / bytes / int 协议格式转换
│   ├── packet.py          # PacketData 构建/解析 + 包体加解密
│   ├── session.py         # 淘米认证 (account-co.61.com) -> session
│   ├── ws_client.py       # 标准库最小 WebSocket 客户端 (socket/ssl)
│   └── client.py          # SeerClient：连接/登录/心跳流程
├── requirements.txt       # 说明：无第三方依赖
└── refs/                  # 下载的参考帖文与源码
```

---

## 运行

### 0) 环境

仅需 **Python ≥ 3.8**，无第三方依赖。

### 1) 离线自检（不联网）

验证算法、封包、加解密、JSONP 解析是否正确：

```bash
python3 login_test.py --self-test
```

### 2) 干运行（不联网）

验证登录封包能否正常构建（不访问服务器）：

```bash
python3 login_test.py --account 1234567890 --password 你的密码 --dry-run
```

### 3) 真实登录测试

使用你自己的米米号与密码。**密码 `!` `.` `@` `$` 等特殊字符会被 shell 解释**（例如 bash
里 `!` 触发历史展开），所以**不要**直接把它们写在命令行里，推荐下列任一种方式：

**方式一：环境变量（推荐）**

```bash
export SEER_ACCOUNT=你的米米号
export SEER_PASSWORD='p@ss!w.rd'   # 用单引号包裹, 防止 ! 被历史展开
python3 login_test.py
```

**方式二：密码文件**

```bash
printf 'p@ss!w.rd\n' > pass.txt   # 首行为密码, 自动去除换行
python3 login_test.py --account 你的米米号 --password-file pass.txt
```

**方式三：交互输入（密码不回显）**

```bash
python3 login_test.py --account 你的米米号
# 之后按提示输入密码, 输入时不显示
```

**方式四：直接传参（仅当密码不含特殊字符时）**

```bash
python3 login_test.py --account 你的米米号 --password '你的密码'
```

输出示例（分步骤）：

```
=== 赛尔号登录测试 ===

米米号: 1234567890 | 密码: ******

✅  [PASS] 步骤1 获取淘米 session — session=xxxxxxxxxxxxxxxx...
✅  [PASS] 步骤2 构建登录封包 — packet=169B cmd=1001
✅  [PASS] 步骤3 连接网关 — ws://xx.xx.xx.xx:xxxx
✅  [PASS] 步骤4 发送登录封包
✅  [PASS] 步骤5 等待登录应答 — cmd=1001 result=...
✅  [PASS] 步骤6 发送心跳/时间校验 — 序列号=0x...
```

### 保持会话存活（--hold）

默认情况下，脚本验证完登录后会**立即断开**连接（所以不会影响正在线的官方客户端）。
如果你希望脚本**登录后不退出、持续心跳，让账号处于被脚本占用的在线状态**，加 `--hold`：

```bash
python3 login_test.py --hold                  # 一直保持, 直到 Ctrl+C
python3 login_test.py --hold --hold-seconds 60   # 保持 60 秒后自动退出
python3 login_test.py --hold --hold-interval 5   # 心跳间隔 5 秒 (默认)
python3 login_test.py --hold --verbose           # 打印每次收到的封包
```

`--hold` 模式下脚本会保持 WebSocket 连接，定期发心跳（cmd 0x3EA），并自动应答服务器的时间同步请求，
直到你按 Ctrl+C 或到达 `--hold-seconds` 时长。**这通常会把另一端在线的官方客户端挤下线**
（单账号在线互斥，具体以服务器策略为准）。

> ℹ️ 注意：`--hold` 是"保持会话"，不等于"进入游戏场景"。它让账号处于协议级在线状态并维持连接，
> 但还没有发送进入全场景/竞技场的后续命令。若需要真正接管游戏内操作，需要继续往下扩展协议。

### 游戏服务器裸 TCP 加密登录（--game-login）

这是**登录器/Flash 客户端**的登录方式。经抓包逆向确认，登录器连的是游戏服务器
（默认 `101.43.19.60:1201`，**裸 TCP**，不是 WebSocket），并且**所有封包（包括登录）
都被 seer 算法加密**（密钥 `!crAckmE4nOthIng:-)`）。登录封包为 `cmd=1001`，
body = `session(16B)` + `"unknown"` + 填充 + `[1,1,1]` + `"flash_taomee"` + 填充。

```bash
python3 login_test.py --game-login            # 用默认服务器 101.43.19.60:1201
python3 login_test.py --game-login --game-host 101.43.19.60 --game-port 1201
python3 login_test.py --game-login --verbose  # 打印每个解密封包
python3 login_test.py --game-login --game-seconds 15   # 读角色数据最多 15 秒
```

它会：获取淘米 session → 连接游戏服务器 → 发送加密登录 `1001` → 收到并解密服务器的
**角色数据**（`cmd=1001` 大包，含 `uid` + 昵称），从而验证"登录器方式"的登录链路。

**会话密钥（登录后自动切换）**：命令号 <1000 的封包是明文，>1000 的用 seer 算法加密。
登录（`cmd=1001`）之后，按参考帖规则自动派生出**会话密钥**，之后所有封包都用它加解密：

```
seed = LOGIN_IN 响应明文最后4字节 (uint)
xor  = seed ^ 米米号
key  = md5(str(xor)).hexdigest()[:10]     # 取 hex 前10位
```

`SeerClient.login_game()` 登录后会调用 `derive_session_key()` 自动切换，因此能够继续解密
登录后的玩法封包（如 `ENTER_MAP`=2001、`LEAVE_MAP`=2002、`40001/40002/...` 等）。

**封包结构参考（明文）**：`[长度(4)][版本(1)][命令号(4)][米米号(4)][序列号(4)][包体]`。
实测示例（命令号 `42399`=MULTI_ITEM_LIST，参数 `1,2600048`）重放得到，与抓包逐字节一致：

```
00 00 00 19  31  00 00 A5 9F  00 00 00 00  00 00 00 00  00 00 00 01 00 27 AC 70
└─长度=25─┘  └版┘  └命令号42399┘  └米米号=0┘  └序列号=0┘ └── 包体: int32(1), int32(2600048) ──┘
```

> 说明：示例中「米米号」「序列号」为 0（裸字段示意）。登录态真实发包时，工具会把 `米米号`
> 填为当前账号、`序列号` 填为按 `MSerial` 计算的序列号（`...A59F 383934A3 00000184 ...`）。


> ⚠️ 这一步会连**真实游戏服务器**。请只用你自己的账号；游戏自动化有账号冻结风险。

---

## 协议调试 WebUI（webui.py）

一个**纯标准库**（`http.server` + SSE）的调试界面，方便人工调试登录与封包：

```
python3 webui.py --host 127.0.0.1 --port 8680
# 浏览器打开 http://127.0.0.1:8680/
```
> 默认端口 `8680`;若被占用,`--port 0` 会自动选一个空闲端口(打印实际端口),或
> `python3 webui.py --port <新端口>` 手动换。

界面三个功能区：

1. **登录操作**：填米米号/密码/游戏服IP/端口，点"登录"。它会：淘米认证（或调用方提供 session 时跳过）→ 连游戏服务器 → 发登录(1001) → **自动派生会话密钥**。
2. **日志输出**：`/api/stream` 用 **SSE** **实时**推送所有日志（登录流程/每个收发封包/**命令名**/命令号/包体/会话密钥），页面自动滚动。**去掉了原来的 1200ms 轮询与 seq 去重限制**，改为按事件实时追加。顶部有过滤栏：
   - **过滤包id**：名单内的包命令号会被**舍弃**（默认 `40002,2192,41228,4047,4475,41080,9134,2604,9019,2101,2004,3405,2601,2002,43321,1002,9908`，可编辑，点"应用过滤"保存；`/api/filter` 读写，**持久化到 `webui_filter.json`**）
   - **接收send / 接收recv**：两个复选框可分别决定是否显示 send 包 与 recv 包
   - 改动过滤或勾选后即时生效（无过滤的已存封包会按新规则重新显示/隐藏）
3. **发包测试**：登录成功后，填"**命令名或命令号** + 包体参数"点"发送"，用当前连接（会话密钥加密）发出一条封包，并读取服务器解密后的应答。命令名来自 `Command.cs` 的字典（`cmdmap.json`，共 2910 条），如 `ENTER_MAP`、`GET_PET_INFO`。命令输入框上方有"**命令（全部）**"下拉，可选全部 2910 条命令（选中即回填命令名，便于发送）；下面文本框也可输入过滤（输入 `ENTER` 会自动补全），发送时按名字解析成命令号。

   **实时监听（最新）**：登录后工具启动**后台监听线程**，实时读取服务器的一切回包；**签发不再阻塞等待应答**——点"发送"只是把包发出去，服务器应答（以及一切 send/recv 封包）随后实时出现在"**服务器响应**"表格里。表格**内容可选中/复制**，列为：`类型`（SEND/RECV）、`命令号`、`包体(hex)`、`十进制数组`；并受**过滤包id**与**接收send/接收recv**复选框约束。因此发完 42399 后，你立刻能在表格里看到自己发出与服务器回应的 42399 及其十进制数组。

   **应答转十进制数组（新增）**：每条收到的封包会按标准包体（4 字节大端 int32）拆成**十进制数组**，显示在"服务器响应"表格的最后一列（如 `[1, 2600048, ...]`）；非 int32 对齐的尾部字节单独标注。

   **包体输入（新增）**：包体默认按"**十进制参数列表**"输入，逗号/空格分隔，发送时自动转成标准包体（每个参数按 **4 字节大端 int32** 依次拼接，与 `Command.cs` 参考包一致）。例：填写 `0 10 725 172` 会打包成 `00 00 00 00 00 00 00 0a 00 00 02 d5 00 00 00 ac`（即 ENTER_MAP 的 `[0][地图号][x][y]`）。输入框下方实时预览打包后的十六进制与分包明细。另支持：
   - `h:010203` 直接给原始十六进制字节
   - `b:255` 单个字节
   - `s:文本` 1 字节长度前缀 + UTF-8 文本
   - 勾选"**原样HEX**"则把输入当作十六进制直接发送（用于调试原始封包）

   **查询背包精灵（新增）**：点"**查询背包精灵(43706)**"发送 `43706 GET_PET_INFO_BY_ONCE`（空包体），后台监听线程读取应答后，用 `seer/petinfo.py` 的 `parse_front()` 按反编译 `PetInfo.as` 布局解析出 `id/名字/等级/经验/天赋/性格/六维(体力/攻击/防御/特攻/特防/速度)/学习力`。**说明**：目前只能可靠解出**第一背包数量**和**第一只精灵**；要按 `[第一背包数][pet1][pet2]...[第二背包数]...` 切割出所有精灵，需要 `PetSkillInfo`/`PetEffectInfo`/`PetResistanceInfo` 的字节布局（它们位于 `PetInfo` 中段，为可变长技能/特性/抗性）。另支持 `2301 GET_PET_INFO` 单只精灵解析（同样用 `parse_front`）。`seer/petinfo.py` 的字段顺序严格对应源码 `PetInfo` 构造函数，能力值（hp/attack/defence/s_a/s_d/speed/dv/nature/level/exp/ev_*）都在技能段之前。

HTTP 接口：

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 调试页面 |
| `/api/login` | POST | `{account,password,host,port,session?}` 发起登录 |
| `/api/send` | POST | `{cmd,body,encode,count,timeout}` 发包并读应答（`cmd` 支持命令名或命令号；`encode="pack"`(默认)把 `body` 当作参数列表打包，`"hex"` 原样十六进制） |
| `/api/cmdmap` | GET | Command.cs 命令名字典（id→name，2910 条），供前端补全 |
| `/api/body-preview` | POST | `{spec}` 把参数列表打包成标准包体，返回 `{hex,length,parts}`（前端实时预览） |
| `/api/status` | GET | 当前状态 |
| `/api/log` | GET | 全部日志 |
| `/api/stream` | GET | SSE 实时日志流 |
| `/api/filter` | GET/POST | 读取/保存过滤包id名单（`{ids:[...]}`，名单内的包舍弃） |
| `/api/disconnect` | POST | 断开当前连接 |

> `session` 字段可省略；提供时可跳过淘米认证（便于对 mock 服务器调试）。

### 分析登录后的封包（--log-file）

当前实现只覆盖了"网关认证"层（登录 1001 + 心跳）。要**真正顶掉官方客户端 / 进入游戏**，
需要网关之后的"进游戏"封包序列——这部分不在公开参考里，需要抓真实客户端的报文来分析。
本工具可以把你登录后**收到/发出的每个封包**完整记录到文件，方便排查缺口：

```bash
python3 login_test.py --hold --log-file packets.txt --verbose
```

`packets.txt` 会记录每一帧的 `时间 方向 cmd 包体 完整hex`，例如：

```
12:00:01 SEND cmd=1001 body=0123456789abcdef... full=000000a931000003e9...
12:00:02 RECV cmd=1001 body=00000000 full=0000001531000003E9...0000271200000000
12:00:05 SEND cmd=1002 body= full=0000001131000003ea...
```

把这份日志贴给我，我就能据此判断登录后服务端还要求哪些后续命令，并继续实现。

### 参数

| 参数 | 说明 |
| --- | --- |
| `--account` | 米米号（账号）；也可用环境变量 `SEER_ACCOUNT` |
| `--password` | 帐号明文密码；含特殊字符时建议改用环境变量/文件/交互输入 |
| `--password-file` | 从文件读取密码（取首行，自动去除换行） |
| `--auth-url` | 覆盖淘米认证接口（默认 `https://account-co.61.com/...`） |
| `--gateway` | 覆盖网关入口 URL（默认 `https://seerh5login.61.com/online_gate`） |
| `--connect-url` | 直接指定 WebSocket 地址，跳过网关解析 |
| `--log-file` | 把每个收发封包（时间/方向/cmd/完整hex）写入文件，供分析 |
| `--hold` | 登录成功后保持连接并持续心跳（不立即断开） |
| `--hold-seconds` | 保持连接的秒数，0=直到 Ctrl+C |
| `--hold-interval` | 心跳间隔（秒），默认 5 |
| `--timeout` | 网络超时（秒，默认 15） |
| `--ack-timeout` | 等待登录应答超时（秒，默认 12） |
| `--dry-run` | 不联网，仅本地构建封包 |
| `--self-test` | 仅运行算法/封包离线自检 |
| `--verbose` | 打印完整错误堆栈 |

**凭证来源优先级**：命令行参数 > 环境变量 `SEER_ACCOUNT`/`SEER_PASSWORD` > `--password-file` >
交互输入（`getpass`，密码不回显，也不会出现在进程参数里）。

---

## 说明与注意事项

1. **协议可能随版本变化**：赛尔号是 Flash/H5 客户端，登录与网关参数、密钥、封包体
   都可能随版本更新。若真实登录失败，请重新抓包比对 `refs/` 里的资料并更新
   `seer/client.py` 中的封包体、`seer/algorithm.py` 中的密钥、`seer/session.py`
   中的认证参数。
2. **序列号十分重要**：服务器会校验每个封包的 `result`（序列号）字段。本项目按
   seerNew 的 `MSerial` 计算，若服务器行为有变需同步调整。
3. **明文密码仅用于本地**：密码只在本地计算 MD5 后发送，不会以明文落盘；但仍建议
   仅在受控环境运行，勿在他人机器上使用。
4. **合规**：请遵守淘米/赛尔号服务条款，仅测试自己的账号。

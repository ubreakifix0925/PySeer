# 赛尔号游戏本体抓包分析 — `refs/gamedump4.txt`

> 本文梳理「结合仓库现有资料 + 工具，把 `refs/gamedump4.txt` 这份赛尔号**游戏本体**抓包
> 还原成可读的协议封包」的完整流程与结论。核心分析器为
> [`tools/analyze_gamedump.py`](./tools/analyze_gamedump.py)，产物
> [`gamedump4_decoded.txt`](./gamedump4_decoded.txt)、[`gamedump4_named.txt`](./gamedump4_named.txt)、
> [`gamedump4_cmds.txt`](./gamedump4_cmds.txt) 与
> [`gamedump4_cmds.csv`](./gamedump4_cmds.csv)（与既有 `postlogin_decoded.txt` /
> `postlogin_named.txt` 同风格）。
>
> `gamedump4_cmds.txt` 按 README 的封包结构，把每条解析出的命令输出为
> `命令号 | 包体(hex) | 包体十进制数组(4 字节大端 int32)`，十进制数组用
> `seer/body.py::decode_body` 切分，与 WebUI「服务器响应」表的十进制数组列一致。
> `gamedump4_cmds.csv` 是同等内容的 **CSV**（带 UTF-8 BOM，Excel 可直接打开），列为
> `方向/序号/命令号/命令名/uid/序列号/包体长度/包体hex/十进制数组/非对齐尾字节`；
> 超大包体会**截断预览**并标注 `...(共 N 字节 / 共 N 个值)`，以保证单元格不超 Excel 单格上限。

---

## 1. 抓包文件格式

`gamedump4.txt` 是 **UTF-16LE** 文本（Windows 工具导出），每行一条 TCP 记录，
**Tab 分隔**：

```
<序号>	<时间>	<srcIP>	<dstIP>	<srcPort>	<dstPort>	<hex载荷>
```

- 时间字段为相对毫秒，同一连接内单调递增，用于排序。
- 载荷是十六进制字符串（该次 send/recv 的字节）。
- 共 23741 行，涉及 75 条 TCP 连接；其中大量是 CDN/资源（`183.216.162.106:443` 等），
  少数是游戏逻辑连接。

关键连接（客户端 `192.168.3.29`）：

| 连接 | 作用 |
|---|---|
| `101.43.19.60:1201` | **游戏服务器（裸 TCP + seer 加密封包）**，客户端端口 60485 |
| `101.43.19.60:1201`（60483） | Flash socket-policy 握手（`<policy-file-request/>`），非游戏协议 |
| `36.155.213.142:8080` / `51.44.x` | HTTP(S) 网关/资源下载 |

---

## 2. 复原流程（能力还原）

### 2.1 读取
自动识别 UTF-16LE/BE/UTF-8，按 Tab 拆字段，得到 `(label, time, sip, dip, sport, dport, hex)`。

### 2.2 方向重组
抓包工具会在**不同记录里重复/覆盖记录同一段数据**（例如一个大封包既按段记录、
又整体记录一次；或同一帧在 `recv` 与更高层各记一次）。处理：

- 按 `(sip,sport,dip,dport)` 分组，得到每条连接的两个方向。
- 对每个方向：按时间排序后，若**下一条记录的字节以本条记录为前缀**，判定本条为
  冗余覆盖并剔除；然后顺序拼接（见 `_reassemble_one_direction`）。
- 这样得到该方向的连续字节流（TCP 应用层流）。

### 2.3 切帧
赛尔号封包在 TCP 流上以「**4 字节大端长度前缀**」自描述：

```
wire = [4B 总长度(含本字段)][密文]
```

长度非法时跳过 1 字节以复位；同一时间戳归并到承载该帧开始字节的记录。

### 2.4 解密（关键）
密文用 seer 算法解密（`seer/algorithm.py::Decrypt`，位移 + XOR，
密钥 `!crAckmE4nOthIng:-)`），得到明文：

```
[ver(1)][cmd(4)][uid(4)][res(4)][body...]
```

**会话密钥**：登录(1001)之前用默认密钥；收到服务器的 **1001 角色数据**（约 7KB）后，
按参考帖规则派生会话密钥并自动切换，**之后所有封包都用它解密**：

```
seed = LOGIN_IN 响应明文最后 4 字节(uint)
xor  = seed ^ 米米号
key  = md5(str(xor)).hexdigest()[:10]
```

> 脚本在解密前会把全局密钥切换为对应密钥（`_alg._key = key`），因为
> `Decrypt` 使用的是模块级全局密钥。

### 2.5 判定方向 / 过滤
- 以「是否出现 cmd=1001 登录帧」识别游戏连接；
- 以「cmd==1001 且包体较大(>256B)」识别**服务器登录响应**（S2C），
  「cmd==1001 且包体较小」识别**客户端登录**（C2S）；内网 IP 兜底。
- 用密钥无法解出合理命令号（>1000000，通常是乱码）的帧，判定为抓包冗余并剔除，
  计入噪声。

---

## 3. 分析结果

### 3.1 会话信息
```
游戏连接: 192.168.3.29:60485 <-> 101.43.19.60:1201
米米号(uid): <账号>
会话密钥(session_key): e10e6f7cd2
C2S 帧数=595  解密命令数=594  (噪声=1)
S2C 帧数=809  解密命令数=809  (噪声=0)
```

### 3.2 登录流程
- C2S `1001 LOGIN_IN` 包体 = `session(16B) + "unknown" + 填充 + [1,1,1] + "flash_taomee" + 填充`
  （共 124 字节），与 `seer/client.py::GAME_LOGIN_TAIL` 一致 → 属于**登录器/Flash 客户端**
  的裸 TCP 加密登录方式。
- S2C `1001 LOGIN_IN`（约 7KB）＝角色数据，客户端据此派生会话密钥。

### 3.3 高频/代表性命令（与 `postlogin_named.txt` 高度一致）

**客户端→服务器：**
```
2301  GET_PET_INFO      x442    # 刷背包/精灵详情的主导命令
2051  GET_SIM_USERINFO  x37
40002 USER_FOREVER_VALUE x26
2304  PET_RELEASE      x24     # 释放/取出精灵
3405  ACTIVEACHIEVE    x19
43706 GET_PET_INFO_BY_ONCE x6  # 整批查询背包精灵
4475  ITEM_LIST        x5
42023 BATCH_GET_BITSET x4      # 大批量数据位集(大包)
46046 GET_MULTI_FOREVER x4
```

**服务器→客户端：**
```
2301  GET_PET_INFO      x458
2051  GET_SIM_USERINFO  x53
40002 USER_FOREVER_VALUE x36
2304  PET_RELEASE      x24
3405  ACTIVEACHIEVE    x23
3404  SETTITLE        x19     # 称号同步
2604  CHANGE_CLOTH    x17     # 服装同步
4475  ITEM_LIST        x14
42023 BATCH_GET_BITSET x12
2001  ENTER_MAP        x3      # 进地图(服务端推送)
2002  LEAVE_MAP        x2
2003  LIST_MAP_PLAYER  x1
2101  PEOPLE_WALK      x6
```

### 3.4 数据的正确性验证（最重要）

解密后的包体能被**从反编译源码移植的解析器**正常解析，证明还原是真实游戏数据，
而非乱码：

- `GET_PET_INFO(2301)` 包体 → `seer/petinfo.py::parse_full` 解出：
  `id=5000 等级=100 天赋=31 性格=8 体力=584/584 攻击=230 防御=276 特攻=439 特防=277 速度=316`。
- `GET_PET_INFO_BY_ONCE(43706)` 包体 → `split_petbag_43706` 解出 `first_count=6`,
  `second_count=6`，每只都是真实精灵（`id=4648/3577` 等，等级 100、天赋 31）。

> 名字字段在包内常被服务器留空，客户端从本地 `PetXMLInfo` 查——因此这里显示
> `(id=xxx)`，与现有 `petinfo.py` 的 `resolve_name` 行为一致。

### 3.5 有趣的命令
- `FUCK_SHINEHOO_TIMES(2707)`：客户端对某活动次数的查询（命名来自逆向字典）。
- `BATCH_GET_BITSET(42023)`：客户端一次拉取数千位（`body(8048)`），是典型的
  「一次性同步大量开关/进度」命令。
- `ENTER_MAP/LEAVE_MAP` 只在服务器→客户端方向出现：进图/离图由服务器主导推送。

---

## 4. 复现

```bash
# 完整分析并输出解码结果 + 命令名统计
python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt \
    --out-decoded gamedump4_decoded.txt --out-named gamedump4_named.txt

# 输出 命令号 | 包体hex | 包体十进制数组 (README 标准体, 4字节大端 int32)
python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt --out-cmds gamedump4_cmds.txt

# 输出 CSV (方向/序号/命令号/命令名/uid/序列号/包体hex/十进制数组)，Excel 可直接打开
python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt --out-csv gamedump4_cmds.csv

# 只看前若干条
python3 tools/analyze_gamedump.py --dump refs/gamedump4.txt --show-c2s 8 --show-s2c 10
```

输出文件 `gamedump4_decoded.txt`：逐条 `cmd/名称/uid/res/body(长度/hex/ascii)`；
`gamedump4_named.txt`：按方向统计的去重命令号及次数；
`gamedump4_cmds.txt`：按方向逐条 `cmd / body(hex) / ints(十进制数组)`；
`gamedump4_cmds.csv`：CSV 版（`direction,index,cmd,cmd_name,uid,result,body_len,body_hex,ints,remainder`）。

---

## 5. 依赖与边界

- 仅 Python 标准库，复用本仓库 `seer/`（`algorithm.Decrypt`）、`cmdmap.json`、`seer/petinfo.py`。
- 关键算法（位移+XOR、序列号、会话密钥派生、封包头结构）来自
  `refs/seerpacket/{Algorithm,SendPacket,Misc,Command}.cs` 与
  `refs/seerNew/`，与 52pojie `thread-1468888`/`thread-2053139` 的逆向结论一致。
- 若服务器端协议或密钥随版本变化，需同步更新 `seer/algorithm.py` 的密钥与
  `run_full` 中「大小登录判定」的阈值。

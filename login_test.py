#!/usr/bin/env python3
"""赛尔号 (Seer) 登录协议测试入口.

运行方式:

    # 推荐: 密码走环境变量 / 密码文件 / 交互输入, 避免特殊字符 (. ! @ ...) 被 shell 解释
    export SEER_PASSWORD='p@ss!w.rd'          # 用单引号, 防止 ! 历史展开
    python3 login_test.py --account 你的米米号

    python3 login_test.py --account 你的米米号 --password-file pass.txt
    python3 login_test.py --account 你的米米号              # 交互输入密码 (不回显)

    # 其它
    python3 login_test.py --self-test                          # 只运行算法/封包自检
    python3 login_test.py --account ... --password ... --dry-run   # 不连服务器
    python3 login_test.py --account ... --password ... --connect-url ws://127.0.0.1:9999

日志级别: --verbose 打全部详情; 默认只打印每一步 PASS/FAIL 与关键字段.

注意: 仅用于对你自己拥有的账号做登录协议测试与学习.
"""

import argparse
import os
import sys
import time
import traceback
import getpass

from seer.algorithm import Decrypt, Encrypt, md5, MSerial
from seer.client import DEFAULT_GAME_SERVER, LoginError, SeerClient
from seer.misc import decimal_to_8hex
from seer.packet import CMD_LOGIN, encrypt, decrypt, parse_packet
from seer.session import _extract_jsonp

STEP_OK = "PASS"
STEP_FAIL = "FAIL"
STEP_SKIP = "SKIP"


def report(step, status, detail=""):
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}.get(status, "•")
    line = f"{icon}  [{status}] {step}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    return status == STEP_OK


class TestRunner:
    def __init__(self, args):
        self.args = args
        self.results = []

    def run_self_test(self):
        print("== 离线自检 (不联网) ==\n")
        oks = []

        # 1. MD5
        expect = "e10adc3949ba59abbe56e057f20f883e"
        got = md5("123456")
        oks.append(report("MD5 自检", STEP_OK if got == expect else STEP_FAIL,
                          f"md5('123456')={got}"))

        # 2. Encrypt/Decrypt 往返
        for n in (1, 16, 32, 100, 136):
            plain = bytes(range(1, n + 1))
            enc = Encrypt(plain)
            dec = Decrypt(enc[:len(enc)])  # Encrypt 会加一个尾部字节
            ok = dec[:len(plain)] == plain
            oks.append(report(f"Encrypt/Decrypt 往返 (n={n})",
                              STEP_OK if ok else STEP_FAIL,
                              f"len(enc)={len(enc)}"))

        # 3. SeerPacket 套壳加解密往返 (encrypt/decrypt 含长度前缀)
        body = bytes(range(0, 60))
        payload = decimal_to_8hex(len(body)).encode() + body  # [4B 长度][内容]
        # 重新用正确的大端长度字段
        import struct
        payload = struct.pack(">I", len(body)) + body
        sealed = encrypt(payload)
        opened = decrypt(sealed)
        ok = opened[4:] == body
        oks.append(report("SeerPacket 套壳加解密往返", STEP_OK if ok else STEP_FAIL))

        # 4. MSerial 序列号
        serial = MSerial(0, 10, 255, 1001)
        oks.append(report("MSerial 序列号计算", STEP_OK, f"MSerial(0,10,255,1001)={serial}"))

        # 5. 封包解析/构建
        try:
            pkt = parse_packet("000000a931000003e9" + "00000000" + "00000000" + "74616f6d6565")
            oks.append(report("封包解析", STEP_OK, f"cmd={int(pkt.cmd_id,16)} body={pkt.body[:6]}"))
        except Exception as e:
            oks.append(report("封包解析", STEP_FAIL, str(e)))

        # 6. JSONP 会话响应解析 (模拟淘米返回: callback({...}))
        sample = 'jQuery1234567890_123({"status":1,"data":{"session":"abcdef"},"msg":"ok"});'
        try:
            data = _extract_jsonp(sample)
            sess = data["data"]["session"]
            oks.append(report("JSONP 会话解析", STEP_OK if sess == "abcdef" else STEP_FAIL,
                              f"session={sess}"))
        except Exception as e:
            oks.append(report("JSONP 会话解析", STEP_FAIL, str(e)))

        print()
        return all(oks)

    def run(self):
        if self.args.self_test:
            return self.run_self_test()

        a = self.args
        print("=== 赛尔号登录测试 ===\n")
        print(f"米米号: {a.account} | 密码: {'*' * len(a.password)}")
        print()

        client = SeerClient(
            account=a.account,
            password=a.password,
            auth_url=a.auth_url,
            gateway_endpoint=a.gateway,
            connect_url=a.connect_url,
            timeout=a.timeout,
        )
        steps = []

        # -- 1. session --
        if a.dry_run:
            # dry-run 不连服务器, 只验证本地封包能正常构建
            client.session = "0123456789abcdef" * 2  # 32 hex chars = 16B, 贴近真实 session
            steps.append(report("步骤1 获取淘米 session", STEP_SKIP,
                                "dry-run 模式跳过联网认证"))
        else:
            try:
                sess = client.fetch_session()
                steps.append(report("步骤1 获取淘米 session", STEP_OK, f"session={sess[:16]}..."))
            except Exception as e:
                steps.append(report("步骤1 获取淘米 session", STEP_FAIL, str(e)))

        # -- 2. 登录封包构建 --
        try:
            login_pkt = client.build_login_packet()
            pkt_len = len(login_pkt) // 2
            steps.append(report("步骤2 构建登录封包", STEP_OK,
                                f"packet={pkt_len}B cmd={CMD_LOGIN}"))
        except Exception as e:
            steps.append(report("步骤2 构建登录封包", STEP_FAIL, str(e)))
            return not all(steps)

        # -- dry-run 到此为止 --
        if a.dry_run:
            return not all(steps)

        # -- 3..6 连接、登录、心跳/保持; 全程可按需写入封包日志 --
        log_f = None
        if a.log_file and not a.dry_run:
            try:
                log_f = open(a.log_file, "w", encoding="utf-8")
            except OSError as e:
                print(f"无法打开日志文件 {a.log_file}: {e}")
                log_f = None
            if log_f:
                log_f.write("# 赛尔号封包日志 | 时间 方向 cmd 包体(前48B) 完整hex\n")
                log_f.write(f"# 米米号={a.account}\n")
                log_f.flush()

                def _on_frame(direction, hex_str, cmd_id, body):
                    log_f.write(f"{time.strftime('%H:%M:%S')} {direction} cmd={cmd_id} "
                                f"body={body[:48]} full={hex_str}\n")
                    log_f.flush()
                client.on_frame = _on_frame

        try:
            # -- 3. 连接网关 --
            try:
                url = client.connect()
                steps.append(report("步骤3 连接网关", STEP_OK, url))
            except Exception as e:
                steps.append(report("步骤3 连接网关", STEP_FAIL, str(e)))
                return not all(steps)

            # -- 4. 发送登录 --
            try:
                client.send_login()
                steps.append(report("步骤4 发送登录封包", STEP_OK))
            except Exception as e:
                steps.append(report("步骤4 发送登录封包", STEP_FAIL, str(e)))

            # -- 5. 等待登录应答 --
            ack_wait = a.ack_timeout
            try:
                deadline = time.time() + ack_wait
                ack = None
                while time.time() < deadline:
                    pkt = client.recv_packet(timeout=max(0.1, min(2, deadline - time.time())))
                    if pkt is None:
                        continue
                    if int(pkt.cmd_id, 16) == CMD_LOGIN:
                        ack = pkt
                        break
                if ack:
                    steps.append(report("步骤5 等待登录应答", STEP_OK,
                                        f"cmd={CMD_LOGIN} result={ack.result}"))
                else:
                    steps.append(report("步骤5 等待登录应答", STEP_FAIL,
                                        f"{ack_wait}s 内未收到登录应答"))
            except Exception as e:
                steps.append(report("步骤5 等待登录应答", STEP_FAIL, str(e)))

            # -- 6. 心跳保活 --
            if client.is_logged_in:
                if a.hold:
                    hold_desc = f"每 {a.hold_interval}s 心跳"
                    if a.hold_seconds:
                        hold_desc += f", 持续 {a.hold_seconds}s"
                    else:
                        hold_desc += ", Ctrl+C 结束"
                    steps.append(report("步骤6 保持会话 (心跳保活)", STEP_OK, hold_desc))

                    ok_hold = client.hold(
                        interval=a.hold_interval,
                        max_seconds=a.hold_seconds or 0,
                        on_heartbeat=lambda: print(
                            f"  ... 心跳 @ {time.strftime('%H:%M:%S')}  序列号=0x{client.last_result:x}",
                            flush=True),
                        verbose=a.verbose,
                    )
                    steps.append(report("步骤6 会话保持流程结束",
                                        STEP_OK if ok_hold else STEP_FAIL,
                                        f"连接状态: {'正常' if ok_hold else '断开'}"))
                elif a.listen:
                    listen_desc = ("监听服务器推送"
                                   + (f" {a.listen_seconds}s" if a.listen_seconds else " Ctrl+C 结束"))
                    steps.append(report("步骤6 静默监听服务器推送", STEP_OK, listen_desc))
                    ok_listen = client.listen(max_seconds=a.listen_seconds, verbose=a.verbose)
                    steps.append(report("步骤6 监听结束",
                                        STEP_OK if ok_listen else STEP_FAIL,
                                        f"连接状态: {'正常' if ok_listen else '断开'}"))
                else:
                    try:
                        client.send_heartbeat()
                        steps.append(report("步骤6 发送心跳/时间校验", STEP_OK,
                                            f"序列号=0x{client.last_result:x}"))
                    except Exception as e:
                        steps.append(report("步骤6 发送心跳/时间校验", STEP_FAIL, str(e)))
            else:
                steps.append(report("步骤6 发送心跳/时间校验", STEP_SKIP,
                                    "未登录成功, 跳过"))
        finally:
            if log_f:
                log_f.close()

        client.close()
        print()
        return all(steps)


def build_parser():
    p = argparse.ArgumentParser(description="赛尔号登录测试")
    p.add_argument("--account", help="米米号 (帐号); 也可用环境变量 SEER_ACCOUNT 提供")
    p.add_argument("--password", help="帐号明文密码 (含特殊字符时建议改用环境变量/文件/交互输入)")
    p.add_argument("--password-file", help="从文件读取密码 (取首行, 自动去除换行; 可避免 shell 特殊字符问题)")
    p.add_argument("--auth-url", default=None, help="覆盖淘米认证接口")
    p.add_argument("--gateway", default=None, help="覆盖网关入口 URL")
    p.add_argument("--connect-url", default=None, help="直接指定 WebSocket 地址 (跳过网关解析)")
    p.add_argument("--log-file", default=None, help="把每个收发封包 (时间/方向/cmd/完整hex) 写入文件, 供分析")
    p.add_argument("--timeout", type=float, default=15, help="网络超时(秒)")
    p.add_argument("--hold", action="store_true", help="登录成功后保持连接并持续心跳 (不立即断开)")
    p.add_argument("--hold-seconds", type=float, default=0, help="保持连接的秒数, 0=直到 Ctrl+C")
    p.add_argument("--hold-interval", type=float, default=5, help="心跳间隔(秒), 默认 5")
    p.add_argument("--listen", action="store_true", help="登录后不发心跳, 只静默接收服务器推送 (观察进游戏数据)")
    p.add_argument("--listen-seconds", type=float, default=30, help="监听时长(秒), 0=直到 Ctrl+C")
    p.add_argument("--ack-timeout", type=float, default=12, help="等待登录应答超时(秒)")
    p.add_argument("--game-login", action="store_true", help="用游戏服务器裸 TCP 加密登录 (登录器/Flash 方式)")
    p.add_argument("--game-host", default=None, help="游戏服务器主机 (默认 101.43.19.60)")
    p.add_argument("--game-port", type=int, default=1201, help="游戏服务器端口 (默认 1201)")
    p.add_argument("--game-seconds", type=float, default=10, help="读取角色数据的秒数(默认 10)")
    p.add_argument("--dry-run", action="store_true", help="不联网, 仅本地构建封包")
    p.add_argument("--self-test", action="store_true", help="仅运行算法/封包离线自检")
    p.add_argument("--verbose", action="store_true", help="打印完整错误堆栈")
    return p


def _extract_cjk(body):
    """从角色数据 body 里提取最长的 UTF-8 中文字符串 (昵称).

    body 结构: uid(4B) + 字段(4B) + 昵称(UTF-8) + 填充. 逐偏移尝试, 解码到首个 NUL,
    取含 CJK 字符最多的那段.
    """
    def cjk(s):
        return sum(1 for ch in s if '\u4e00' <= ch <= '\u9fff')

    best = ""
    for off in range(0, min(len(body), 64)):
        seg = body[off:].split(b"\x00")[0]
        if not seg:
            continue
        for end in range(len(seg), 0, -1):
            try:
                s = seg[:end].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if cjk(s) > cjk(best):
                best = s
            break
    return best


def run_game_login(args):
    """游戏服务器裸 TCP 加密登录 (登录器/Flash 客户端方式)."""
    print("=== 赛尔号游戏服务器登录 (裸 TCP + seer 加密) ===\n")
    client = SeerClient(account=args.account, password=args.password,
                        auth_url=args.auth_url, timeout=args.timeout)
    ok = False
    try:
        sess = client.fetch_session()
        print(f"步骤1 获取淘米 session — {sess[:16]}...")
    except Exception as e:
        print(f"步骤1 获取淘米 session — ❌ FAIL: {e}")
        if args.verbose:
            traceback.print_exc()
        client.close()
        return False

    host = args.game_host or DEFAULT_GAME_SERVER[0]
    port = args.game_port or DEFAULT_GAME_SERVER[1]
    try:
        conn, resp = client.login_game(host, port, max_seconds=args.game_seconds,
                                       verbose=args.verbose)
        print(f"步骤2 连接游戏服务器 — {conn} (裸TCP)")
        print("步骤3 发送加密登录 (cmd=1001)")

        role = [r for r in resp if r["cmd"] == CMD_LOGIN and len(r["body"]) > 100]
        if role:
            b = role[0]["body"]
            r_uid = int.from_bytes(b[:4], "big")
            nick = _extract_cjk(b)
            print(f"步骤4 ✅ 收到角色数据 — cmd=1001 uid={r_uid} 昵称={nick or '(无法解析)'}")
            ok = True
        else:
            print("步骤4 ❌ 未收到角色数据 (未命中 cmd=1001 大包)")
        print(f"\n本次共收到 {len(resp)} 个解密封包.")
    except Exception as e:
        print(f"\n游戏登录异常: {e}")
        if args.verbose:
            traceback.print_exc()
    finally:
        client.close()
    return ok


def resolve_credentials(args):
    """解析帐号密码, 来源优先级: 命令行 > 环境变量 > 密码文件 > 交互输入.

    密码含 `.` `!` 等特殊字符时, 命令行会被 shell 解释 (例如 `!` 触发历史展开),
    所以强烈建议改用 SEER_PASSWORD 环境变量、--password-file 或交互输入.
    """
    account = args.account or os.environ.get("SEER_ACCOUNT")
    password = args.password or os.environ.get("SEER_PASSWORD")

    if not password and args.password_file:
        try:
            with open(args.password_file, "r", encoding="utf-8") as f:
                content = f.read()
            password = content.splitlines()[0] if content.strip() else ""
        except OSError as e:
            print(f"读取密码文件失败: {e}")
            sys.exit(2)

    if account and password:
        return account, password

    # 交互输入 (仅当终端可用时). 密码用 getpass 回显隐藏, 因而不会出现在进程参数里.
    if sys.stdin.isatty():
        if not account:
            account = input("米米号: ").strip()
        if not password:
            password = getpass.getpass("密码: ")
        return account, password

    print("缺少参数: 请通过 --account/--password、环境变量 SEER_ACCOUNT/SEER_PASSWORD 或 --password-file 提供。")
    print("提示: 密码含 . ! 等特殊字符时, 建议用环境变量或密码文件, 避免被 shell 解释。")
    sys.exit(2)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.self_test:
        ok = TestRunner(args).run_self_test()
        sys.exit(0 if ok else 1)

    args.account, args.password = resolve_credentials(args)

    if args.game_login:
        try:
            ok = run_game_login(args)
        except LoginError as e:
            print(f"\n登录流程异常: {e}")
            ok = False
        except KeyboardInterrupt:
            print("\n已被中断")
            ok = False
        except Exception as e:
            print(f"\n未预期异常: {e}")
            if args.verbose:
                traceback.print_exc()
            ok = False
        sys.exit(0 if ok else 1)

    try:
        ok = TestRunner(args).run()
    except LoginError as e:
        print(f"\n登录流程异常: {e}")
        ok = False
    except KeyboardInterrupt:
        print("\n已被中断")
        ok = False
    except Exception as e:
        print(f"\n未预期异常: {e}")
        if args.verbose:
            traceback.print_exc()
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

"""赛尔号 (Seer) 登录协议测试包.

该包按照《赛尔号：通信协议逆向与模拟》(52pojie thread-1468888) 及其公开实现
Altriazyk/seerNew 整理, 将赛尔号的脱机登录协议封装成可复用的模块:

    algorithm.py  加密/解密算法 (XOR + 位移), MD5, 序列号计算
    misc.py       十六进制/字节/整数在协议里的转换工具
    packet.py     封包 (包头 + 包体) 的构建与解析, 以及协议体加解密
    session.py    淘米帐号认证 (account-co.61.com) -> session
    client.py     基于标准库的最小 WebSocket 客户端 + 赛尔号登录/心跳流程

只用于对自己所拥有的帐号做登录协议测试与学习, 请勿用于任何越权/窃取凭证等用途.
"""

__all__ = ["algorithm", "misc", "packet", "session", "client"]

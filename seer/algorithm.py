"""赛尔号协议用到的加密算法与序列号计算.

算法来源: 《赛尔号：通信协议逆向与模拟》(52pojie thread-1468888) 的逆向结果,
与公开实现 Altriazyk/seerNew / iyzyi/SeerPacket 一致. 该算法是一个 "位移 + XOR"
的非对称简单算法, 用于协议体的加解密 (封包头本身是明文十六进制).

同时这里包含协议用到的普通 MD5 (淘米帐号认证的密码散列) 与序列号函数 MSerial.
"""

import hashlib

# 协议密钥 (来源: 逆向得到的字符串)
KEY_STRING = "!crAckmE4nOthIng:-)"
_key = KEY_STRING.encode("utf-8")


def md5(data: str) -> str:
    """返回 UTF-8 字符串的 MD5 十六进制串 (小写), 用于淘米认证的 passwd 字段."""
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def _advance_key_index(j: int, need_become_zero: bool, key_len: int):
    """复刻 seerNew 中 key 索引的推进逻辑 (含 skip 行为)."""
    if j == 1 and need_become_zero:
        return 0, False
    if j == key_len:
        return 0, True
    return j, need_become_zero


def Decrypt(cipher: bytes) -> bytes:
    """解密协议体 (输入为原始字节, 不含 4 字节长度前缀)."""
    length = len(cipher)
    rotate = _key[(length - 1) % len(_key)] * 13 % length
    cipher = cipher[length - rotate:] + cipher[0:length - rotate]

    plain = bytearray(length - 1)
    for i in range(length - 1):
        plain[i] = ((cipher[i] >> 5) | (cipher[i + 1] << 3)) & 0xFF

    j = 0
    need_become_zero = False
    for i in range(len(plain)):
        if j == 1 and need_become_zero:
            j = 0
            need_become_zero = False
        if j == len(_key):
            j = 0
            need_become_zero = True
        plain[i] = (plain[i] ^ _key[j]) & 0xFF
        j += 1
    return bytes(plain)


def Encrypt(plain: bytes) -> bytes:
    """加密协议体 (输出含一个尾部占位字节, 由调用方决定如何截取)."""
    plain_len = len(plain)
    cipher = bytearray(plain_len + 1)

    j = 0
    need_become_zero = False
    for i in range(plain_len):
        if j == 1 and need_become_zero:
            j = 0
            need_become_zero = False
        if j == len(_key):
            j = 0
            need_become_zero = True
        cipher[i] = (plain[i] ^ _key[j]) & 0xFF
        j += 1

    cipher[plain_len] = 0
    for i in range(len(cipher) - 1, 0, -1):
        cipher[i] = ((cipher[i] << 5) | (cipher[i - 1] >> 3)) & 0xFF
    cipher[0] = ((cipher[0] << 5) | 3) & 0xFF

    rotate = _key[plain_len % len(_key)] * 13 % len(cipher)
    cipher = cipher[rotate:] + cipher[0:rotate]
    return bytes(cipher)


def MSerial(a: int, b: int, c: int, d: int) -> int:
    """序列号函数: 用于生成/校验每个封包的合法性序列号."""
    return a + c + int(a / (-3)) + b % 17 + d % 23 + 120

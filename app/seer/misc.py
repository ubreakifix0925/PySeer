"""十六进制 / 字节 / 整数之间的协议格式转换工具.

赛尔号协议中的封包 (packet) 在传输层表现为裸字节, 但在编写和调试时通常以
"十六进制字符串" 呈现: 包头每个字段 (长度/版本/命令号/用户号/序列号) 都是
"用十六进制 ASCII 文本表示的数字". 因此这里提供与 seerNew/Seer 一致的
转换函数, 方便在一串 hex 字符串与 bytearray 之间来回切换.
"""

import binascii


def hex_to_bytearray(hex_string: str) -> bytearray:
    """gather a hex string (optionally with stray chars) into a bytearray.

    这是协议里最常用的转换: 把 '000000a9' 这样的十六进制字符串变成 bytes.
    非十六进制字符会被忽略, 因此可以直接喂给包含 0x 或空格的字符串.
    """
    cleaned = "".join(c for c in hex_string if c in "0123456789abcdefABCDEF")
    if len(cleaned) % 2 != 0:
        raise ValueError("十六进制字符串长度必须为偶数")
    return bytearray(binascii.unhexlify(cleaned))


def binary_to_hex(binary) -> str:
    """bytearray/bytes -> 大写十六进制字符串 (与 seerNew 输出格式一致)."""
    return binascii.hexlify(bytes(binary)).decode("ascii").upper()


def decimal_to_8hex(decimal_number: int) -> str:
    """整数 -> 8 位十六进制字符串 (小于等于 0xFFFFFFFF)."""
    if decimal_number < 0:
        raise ValueError("不支持负数")
    return "{:08x}".format(decimal_number)


def int_to_hex(value: int) -> str:
    """整数 -> 最小长度十六进制字符串 (不带 0x), 用于 uid 等字段."""
    return "{:x}".format(value)


def get_int_param(data, index: int) -> int:
    """读取协议体前 4 字节作为大端整数 (长度字段).

    seerNew 的实现是先反转 bytearray 再按 little-endian 解析, 效果等于按
    big-endian 直接从 bytes 读取, 这里用更直白的方式表达.
    """
    return int.from_bytes(bytes(data[index:index + 4]), byteorder="big")


def packs_int32(value: int) -> bytes:
    """整数值 -> 大端 4 字节 (协议体长度字段使用)."""
    return int(value).to_bytes(4, byteorder="big")


# ---- 兼容 seerNew 的命名 ----
string_to_bytearray = hex_to_bytearray

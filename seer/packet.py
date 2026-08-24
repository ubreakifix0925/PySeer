"""赛尔号封包 (packet) 的构建、解析与协议体加解密封装.

封包格式 (每个字段都是十六进制 ASCII 文本):

    +--------+------+--------+--------+--------+--------+
    | length | ver  | cmdId  | userId | result | body   |
    | 4B     | 1B   | 4B     | 4B     | 4B     | ...    |
    +--------+------+--------+--------+--------+--------+

在网络层是裸字节; 在本项目中以十六进制字符串表示, 便于读写与核对.
"""

from .algorithm import Decrypt, Encrypt, MSerial
from .misc import binary_to_hex, decimal_to_8hex, get_int_param, hex_to_bytearray, packs_int32

# 包头固定长度 (字节): length(4) + version(1) + cmdId(4) + userId(4) + result(4)
HEADER_BYTES = 17

# 常用命令号
CMD_LOGIN = 0x3E9     # 1001 登录
CMD_TIME_CHECK = 0x3EA  # 1002 时间校验 (客户端用它回应服务器)


class PacketData:
    """一个协议封包: 记录头字段与包体 (均为十六进制字符串)."""

    def __init__(self, length, version, cmd_id, user_id, result, body):
        self.length = length        # 8 hex chars, 封包(头+体)总长
        self.version = version      # 2 hex chars, 版本号
        self.cmd_id = cmd_id        # 8 hex chars, 命令号
        self.user_id = user_id      # 8 hex chars, 米米号
        self.result = result        # 8 hex chars, 序列号
        self.body = body            # 包体 hex 字符串
        self.byte_body = hex_to_bytearray(body)  # 包体字节, 用于序列号与加解密

    def update_length(self):
        total_bytes = self.length_bytes()
        self.length = decimal_to_8hex(total_bytes)

    def length_bytes(self):
        return len(hex_to_bytearray(self.to_hex()))

    def to_hex(self):
        """按协议顺序拼接为十六进制字符串."""
        return f"{self.length}{self.version}{self.cmd_id}{self.user_id}{self.result}{self.body}"


def parse_packet(hex_string: str) -> PacketData:
    """从收到的十六进制封包串解析出 PacketData."""
    if len(hex_string) < HEADER_BYTES * 2:
        raise ValueError("封包过短, 无法解析: %r" % hex_string)
    return PacketData(
        length=hex_string[0:8],
        version=hex_string[8:10],
        cmd_id=hex_string[10:18],
        user_id=hex_string[18:26],
        result=hex_string[26:34],
        body=hex_string[34:],
    )


def compute_result(last_result: int, byte_body, cmd_id_hex: str) -> int:
    """序列号 = MSerial(上一个序列号, 包体长度, 包体异或值, 命令号)."""
    x = 0
    for b in byte_body:
        x ^= (b & 0xFF)
    return MSerial(last_result, len(byte_body), x, int(cmd_id_hex, 16))


# ---- 协议体加解密封装 (带 4 字节长度前缀的完整 "套壳" 数据) ----

def decrypt(encrypted_payload: bytes) -> bytes:
    """解密带长度前缀的协议体: [4B 密文长度][密文] -> [4B 明文长度][明文]."""
    if len(encrypted_payload) < 5:
        raise ValueError("密文过短")
    cipher_len = get_int_param(encrypted_payload, 0)
    plain_len = (cipher_len - 1).to_bytes(4, byteorder="big")
    plain = Decrypt(bytes(encrypted_payload[4:]))
    return plain_len + plain


def encrypt(plain_payload: bytes) -> bytes:
    """加密带长度前缀的协议体: [4B 明文长度][明文] -> [4B 密文长度][密文]."""
    if len(plain_payload) < 4:
        raise ValueError("明文过短, 需包含 4 字节长度字段")
    plain_len = get_int_param(plain_payload, 0)
    cipher_len = (plain_len + 1).to_bytes(4, byteorder="big")
    cipher = Encrypt(bytes(plain_payload[4:]))
    return cipher_len + cipher

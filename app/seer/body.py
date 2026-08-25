"""包体参数打包: 把用户输入的多参数列表转换成标准包体(bytes).

赛尔号协议的"标准包体"就是把各参数按 4 字节大端 int32(mimic Int2ByteArray /
decimal_to_8hex)依次拼接。例如 ENTER_MAP(2001) 的包体 = [0][mapid][x][y],
对应输入 ``0 10 725 172`` -> ``00 00 00 00 00 00 00 0A 00 00 02 D5 00 00 00 AC``。

输入语法(可用逗号或空白分隔, 可混用):
    裸数字 / ``i:N``    -> int32 大端(4字节); 支持负号(补码), 支持 ``0x`` 前缀
    ``b:N``            -> 单字节(0..255)
    ``h:HEX``          -> 原始十六进制字节, 逐个字节直接追加
    ``s:TEXT``         -> 1字节长度 + UTF-8 文本字节
    空串                -> 空包体
"""

from __future__ import annotations


def _int32_be(value: int) -> bytes:
    value = int(value)
    if not (-0x80000000 <= value <= 0xFFFFFFFF):
        raise ValueError(f"数值 {value} 超出 int32 范围 (-2147483648..4294967295)")
    value &= 0xFFFFFFFF
    return value.to_bytes(4, "big")


def pack_body(spec, *, raise_on_error: bool = True):
    """把参数列表字符串打包成标准包体(bytes).

    spec: 参数列表, 如 ``"0 10 725 172"``、``"1,2,3"``、``"h:00000001"``。
    raise_on_error: 为 False 时出错返回 (ok=False, error=msg); True 时抛 ValueError。

    返回 bytes 或 (ok, result) 元组, 由 raise_on_error 决定。
    """
    # 兼容: 若传入的是 bytes, 直接返回
    if isinstance(spec, (bytes, bytearray)):
        body = bytes(spec)
        return body if raise_on_error else (True, body)

    text = (spec or "").strip()
    if not text:
        body = b""
        return body if raise_on_error else (True, body)

    # 按逗号/空白分词 (丢弃空 token)
    tokens = [t for t in text.replace(",", " ").split() if t]

    out = bytearray()
    try:
        for tok in tokens:
            low = tok.lower()
            if low.startswith("s:"):
                # 长度前缀字符串: 1 字节长度 + UTF-8字节
                raw = low[2:].encode("utf-8")
                n = len(raw)
                if n > 255:
                    raise ValueError(f"字符串过长(>255字节): {n}")
                out += bytes([n]) + raw
            elif low.startswith("h:"):
                # 原始十六进制字节
                out += _hex_bytes(low[2:])
            elif low.startswith("b:"):
                # 单字节
                v = int(low[2:], 0)
                if not (0 <= v <= 255):
                    raise ValueError(f"b: 需为 0..255, 收到 {v}")
                out += bytes([v])
            else:
                # int32 大端 (支持 0x 前缀与负数)
                out += _int32_be(int(tok, 0))
    except ValueError as e:
        if raise_on_error:
            raise
        return (False, f"{e} (token={tok!r}, spec={text!r})")

    body = bytes(out)
    return body if raise_on_error else (True, body)


def _hex_bytes(hexstr: str) -> bytes:
    h = "".join(c for c in hexstr if c in "0123456789abcdefABCDEF")
    if len(h) % 2 != 0:
        raise ValueError(f"h: 十六进制长度必须为偶数: {hexstr!r}")
    return bytes.fromhex(h)


def parse_parts(spec: str):
    """用于前端预览: 返回 [(token, 类型, 字节数)] 列表; 出错抛 ValueError."""
    text = (spec or "").strip()
    if not text:
        return []
    tokens = [t for t in text.replace(",", " ").split() if t]
    parts = []
    for tok in tokens:
        low = tok.lower()
        if low.startswith("s:"):
            raw = low[2:].encode("utf-8")
            parts.append((tok, "string", 1 + len(raw)))
        elif low.startswith("h:"):
            parts.append((tok, "hex", len(_hex_bytes(low[2:]))))
        elif low.startswith("b:"):
            parts.append((tok, "byte", 1))
        else:
            parts.append((tok, "int32", 4))
    return parts


def decode_body(body, *, signed: bool = True):
    """把包体字节拆成十进制 int32 数组 (标准包体 = 4字节大端 int32 序列).

    body: bytes 或 hex 串.
    signed=True 按有符号 int32 解读 (负值如 -1 会回读为 -1, 与 pack_body 往返一致);
            =False 按无符号解读 (适合以 ID/数量为主的场合).
    返回 {"ints": [int,...], "remainder": hex, "byte_len": int, "aligned": bool}.
    """
    if isinstance(body, str):
        body = bytes.fromhex(_clean_hex(body))
    body = bytes(body)
    n = len(body)
    full = n // 4
    ints = []
    for i in range(full):
        chunk = body[i * 4:(i + 1) * 4]
        ints.append(int.from_bytes(chunk, "big", signed=signed))
    rem = body[full * 4:]
    return {"ints": ints, "remainder": rem.hex(), "byte_len": n, "aligned": (n % 4 == 0)}


def _clean_hex(s):
    return "".join(c for c in s if c in "0123456789abcdefABCDEF")

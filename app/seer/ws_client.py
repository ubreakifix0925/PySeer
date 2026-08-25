"""基于标准库 socket/ssl 的最小 WebSocket 客户端.

为保持"登录测试"零第三方依赖, 这里手工实现了 RFC6455 的握手与分帧,
足够完成赛尔号协议需要的二进制帧收发、ping/pong 保活与正常关闭.
只支持客户端 (mask 发送帧), 这也是本工程唯一需要的角色.
"""

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


class WebSocketTimeout(WebSocketError):
    """读写超时: 表示 "这一窗口内没有数据", 而不是连接异常."""


class WebSocketClosed(Exception):
    pass


class WSClient:
    """一个同步、阻塞式的最小 WebSocket 客户端."""

    def __init__(self, url: str, timeout: float = 12):
        self.url = url
        self.timeout = timeout
        self._sock = None
        self._connected = False

    # ---- 底层 IO ----
    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout:
                raise WebSocketTimeout("读取超时")
            if not chunk:
                raise WebSocketClosed("连接被对端关闭")
            buf += chunk
        return buf

    # ---- 握手 ----
    def connect(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError(f"不支持的 WebSocket 协议: {parsed.scheme}")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"

        self._sock = socket.create_connection((host, port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            self._sock = ctx.wrap_socket(self._sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://seerh5login.61.com\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(1)
            if not chunk:
                raise WebSocketError("握手响应不完整")
            resp += chunk

        status_line = resp.split(b"\r\n")[0].decode("latin-1")
        if "101" not in status_line:
            raise WebSocketError(f"握手失败: {status_line}")

        headers = {}
        for line in resp.decode("latin-1").split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        expect = base64.b64encode(
            __import__("hashlib").sha1((key + MAGIC).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expect:
            raise WebSocketError("Sec-WebSocket-Accept 校验失败 (秘钥不匹配)")

        self._connected = True

    # ---- 发送帧 (客户端必须 mask) ----
    def _send_frame(self, opcode: int, payload: bytes, mask: bool = True):
        header = bytes([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header += bytes([(0x80 if mask else 0) | length])
        elif length <= 0xFFFF:
            header += bytes([(0x80 if mask else 0) | 126]) + struct.pack(">H", length)
        else:
            header += bytes([(0x80 if mask else 0) | 127]) + struct.pack(">Q", length)
        if mask:
            mask_key = os.urandom(4)
            header += mask_key
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + payload)

    def send_binary(self, data: bytes):
        self._send_frame(0x2, data)

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def send_ping(self, data: bytes = b""):
        self._send_frame(0x9, data)

    # ---- 接收帧 (服务器帧不 mask) ----
    def _recv_frame(self, timeout=None):
        old = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            b0, b1 = self._read_exact(2)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else None
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            return opcode, payload
        finally:
            self._sock.settimeout(old)

    def recv(self, timeout=None):
        """接收一帧数据 (自动响应 ping, 遇 close/握手错误抛异常).

        返回 (opcode, payload); opcode 0x2=二进制, 0x1=文本.
        """
        while True:
            opcode, payload = self._recv_frame(timeout)
            if opcode == 0x9:      # ping -> 回 pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:      # pong
                continue
            if opcode == 0x8:      # close
                self._send_frame(0x8, b"")
                self.close()
                raise WebSocketClosed("对端发送了关闭帧")
            if opcode in (0x0, 0x1, 0x2):
                return opcode, payload
            # 其它控制帧/未知帧忽略
            continue

    def is_open(self):
        return self._connected and self._sock is not None

    def close(self):
        if self._connected:
            try:
                self._send_frame(0x8, b"")
            except Exception:
                pass
        self._connected = False
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

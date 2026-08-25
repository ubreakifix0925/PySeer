"""最小原始 TCP 客户端, 用于赛尔号游戏服务器 (裸 TCP + 长度前缀的 seer 封包).

登录器与 Flash 客户端对游戏服务器用的是原始 socket (不是 WebSocket), 且每个
封包都以 "4 字节大端长度" 为前缀 (长度 = 整个封包字节数). 这里实现带缓冲的读,
能正确处理封包跨 TCP 段 / 多封包合并的情况.
"""

import socket

from .ws_client import WebSocketClosed, WebSocketTimeout


class TCPClient:
    def __init__(self, host: str, port: int, timeout: float = 12):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock = None
        self.buf = b""           # 已读到但尚未消费的缓冲
        self._connected = False

    def connect(self):
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._connected = True

    def is_open(self):
        return self._connected and self.sock is not None

    def send(self, data: bytes):
        if not self.is_open():
            raise WebSocketClosed("未连接")
        self.sock.sendall(data)

    def _fill(self, timeout=None):
        old = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(4096)
        except socket.timeout:
            raise WebSocketTimeout("读取超时")
        finally:
            self.sock.settimeout(old)
        if not chunk:
            raise WebSocketClosed("连接被对端关闭")
        self.buf += chunk

    def _read_exact(self, n: int, timeout=None) -> bytes:
        while len(self.buf) < n:
            self._fill(timeout)
        data = self.buf[:n]
        self.buf = self.buf[n:]
        return data

    def read_packet(self, timeout=None) -> bytes:
        """读取一条长度前缀封包 (>=5 字节), 返回整条 wire 封包 (含 4 字节长度)."""
        head = self._read_exact(4, timeout)
        total = int.from_bytes(head, "big")
        if total < 5 or total > 0x100000:
            raise ValueError(f"非法封包长度: {total}")
        body = self._read_exact(total - 4, timeout)
        return head + body

    def close(self):
        self._connected = False
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

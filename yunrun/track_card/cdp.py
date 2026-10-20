# -*- coding: utf-8 -*-
"""Minimal CDP (Chrome DevTools Protocol) client over a hand-rolled WebSocket.

Used by the card renderer because headless Edge's ``--screenshot`` always captures the
whole window: with a fixed window height the card ended up with a large empty strip
below the content. Driving the browser over CDP lets the page report its own content
height and lets us capture exactly that region.

No third-party dependencies: ``websockets`` is not installed on this machine, and the
subset of RFC6455 needed here is small.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.request
from urllib.parse import urlparse


class WS:
    """Text-frame WebSocket client, enough for request/response CDP calls."""

    def __init__(self, url: str, timeout: float = 300.0) -> None:
        parts = urlparse(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=30)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parts.path} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        self._read_until(b"\r\n\r\n")
        self._id = 0
        self._buffer = b""

    def _read_until(self, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = self.sock.recv(1)
            if not chunk:
                break
            buf += chunk
        return buf

    def _exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv(self) -> str:
        """Read one complete text message, reassembling continuation frames."""
        chunks: list[bytes] = []
        while True:
            first, second = self._exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._exact(8), "big")
            if second & 0x80:      # a server should not mask, but tolerate it
                self._exact(4)
            data = self._exact(length) if length else b""
            if opcode == 0x8:
                raise ConnectionError("closed by peer")
            if opcode in (0x9, 0xA):   # ping/pong
                continue
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None, timeout: float = 280.0) -> dict:
        self._id += 1
        message_id = self._id
        self._send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self._recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(method)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def evaluate(ws: "WS", expression: str, await_promise: bool = True, timeout: float = 120.0):
    """Run JS in the page and return the value (or None)."""
    result = ws.call("Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": await_promise,
        "returnByValue": True,
    }, timeout=timeout)
    return result.get("result", {}).get("value")


def page_ws_url(port: int, timeout: float = 15.0) -> str:
    """Wait for the debugger to expose a page target and return its WebSocket URL."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                targets = json.loads(response.read().decode())
            for target in targets:
                if target.get("type") == "page":
                    return target["webSocketDebuggerUrl"]
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(0.3)
    raise TimeoutError(f"no CDP page target on port {port}: {last}")

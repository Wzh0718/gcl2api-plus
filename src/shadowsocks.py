"""Minimal Shadowsocks AEAD client and SOCKS5 bridge.

The application already speaks HTTP and SOCKS5 through httpx.  This module
keeps the Shadowsocks protocol boundary small: each outbound httpx client can
open a local SOCKS5 listener, while the listener wraps CONNECT streams in the
legacy Shadowsocks AEAD TCP framing used by Clash/Mihomo ``type: ss`` nodes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import quote, unquote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_METHODS = {
    "aes-128-gcm": (16, AESGCM),
    "aes-256-gcm": (32, AESGCM),
    "chacha20-ietf-poly1305": (32, ChaCha20Poly1305),
}
_MAX_CHUNK = 0x3FFF
_TAG_SIZE = 16
_SOCKS5_VERSION = 5


@dataclass(frozen=True)
class ShadowsocksNode:
    name: str
    server: str
    port: int
    method: str
    password: str

    @property
    def key_size(self) -> int:
        return _METHODS[self.method][0]


def _decode_base64(value: str) -> bytes:
    raw = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def shadowsocks_uri(
    *, name: str, server: str, port: int, method: str, password: str
) -> str:
    """Build a SIP002-compatible URI for a legacy AEAD Shadowsocks node."""
    method = method.strip().lower()
    if method not in _METHODS:
        raise ValueError(f"不支持的 Shadowsocks 加密方式: {method}")
    if not server or not 1 <= int(port) <= 65535 or not password:
        raise ValueError("Shadowsocks 节点必须包含有效的 server、port 和 password")
    host = server if ":" not in server or server.startswith("[") else f"[{server}]"
    tag = f"#{quote(name, safe='')}" if name else ""
    return f"ss://{_encode_base64(f'{method}:{password}'.encode('utf-8'))}@{host}:{int(port)}{tag}"


def parse_shadowsocks_url(value: str) -> ShadowsocksNode:
    """Parse SIP002 and legacy base64 ``ss://`` URIs."""
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "ss":
        raise ValueError("Shadowsocks 节点必须使用 ss:// 协议")

    name = unquote(parsed.fragment or "")
    encoded_userinfo = parsed.username or ""
    if parsed.hostname and parsed.port:
        try:
            userinfo = _decode_base64(unquote(encoded_userinfo)).decode("utf-8")
            method, password = userinfo.split(":", 1)
        except Exception as exc:
            raise ValueError("ss:// 节点的加密信息无效") from exc
        server = parsed.hostname
        port = parsed.port
    else:
        # Legacy form: ss://base64(method:password@server:port)#name
        try:
            decoded = _decode_base64(parsed.netloc).decode("utf-8")
            method_password, endpoint = decoded.rsplit("@", 1)
            method, password = method_password.split(":", 1)
            endpoint_url = urlsplit(f"//{endpoint}")
            server = endpoint_url.hostname or ""
            port = endpoint_url.port
        except Exception as exc:
            raise ValueError("ss:// 节点地址无效") from exc

    method = method.strip().lower()
    if method not in _METHODS:
        raise ValueError(
            f"不支持的 Shadowsocks 加密方式: {method}；当前支持 aes-128-gcm、aes-256-gcm、chacha20-ietf-poly1305"
        )
    if not server or port is None or not 1 <= port <= 65535 or not password:
        raise ValueError("Shadowsocks 节点必须包含 server、port、method 和 password")
    return ShadowsocksNode(name=name, server=server, port=port, method=method, password=password)


def _evp_bytes_to_key(password: bytes, key_size: int) -> bytes:
    output = bytearray()
    previous = b""
    while len(output) < key_size:
        previous = hashlib.md5(previous + password).digest()
        output.extend(previous)
    return bytes(output[:key_size])


def _nonce(counter: int) -> bytes:
    return counter.to_bytes(12, "little", signed=False)


def _target_address(host: str, port: int) -> bytes:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode("idna")
        if not 1 <= len(encoded) <= 255:
            raise ValueError("目标域名长度无效")
        return b"\x03" + bytes([len(encoded)]) + encoded + struct.pack(">H", port)
    if isinstance(address, ipaddress.IPv4Address):
        return b"\x01" + address.packed + struct.pack(">H", port)
    return b"\x04" + address.packed + struct.pack(">H", port)


class _AeadStream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, node: ShadowsocksNode):
        self.reader = reader
        self.writer = writer
        key, cipher_cls = _METHODS[node.method]
        self.key_size = key
        self.cipher_cls = cipher_cls
        self.master_key = _evp_bytes_to_key(node.password.encode("utf-8"), key)
        salt = os.urandom(key)
        subkey = self._derive_subkey(salt)
        self.writer.write(salt)
        self.send_cipher = cipher_cls(subkey)
        self.receive_cipher: AESGCM | ChaCha20Poly1305 | None = None
        self.send_counter = 0
        self.receive_counter = 0

    def _derive_subkey(self, salt: bytes) -> bytes:
        return HKDF(
            algorithm=SHA1(),
            length=self.key_size,
            salt=salt,
            info=b"ss-subkey",
        ).derive(self.master_key)

    async def send(self, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + _MAX_CHUNK]
            offset += len(chunk)
            encrypted_length = self.send_cipher.encrypt(
                _nonce(self.send_counter), struct.pack(">H", len(chunk)), None
            )
            self.send_counter += 1
            encrypted_payload = self.send_cipher.encrypt(_nonce(self.send_counter), chunk, None)
            self.send_counter += 1
            self.writer.write(encrypted_length + encrypted_payload)
            await self.writer.drain()

    async def receive(self) -> bytes:
        if self.receive_cipher is None:
            # The server response is a separate encrypted stream with its own
            # salt, subkey and nonce sequence.
            response_salt = await self.reader.readexactly(self.key_size)
            self.receive_cipher = self.cipher_cls(self._derive_subkey(response_salt))
        result = bytearray()
        while True:
            encrypted_length = await self.reader.readexactly(2 + _TAG_SIZE)
            length = struct.unpack(
                ">H", self.receive_cipher.decrypt(_nonce(self.receive_counter), encrypted_length, None)
            )[0]
            self.receive_counter += 1
            if length > _MAX_CHUNK:
                raise ValueError("Shadowsocks 数据块长度无效")
            encrypted_payload = await self.reader.readexactly(length + _TAG_SIZE)
            result.extend(
                self.receive_cipher.decrypt(_nonce(self.receive_counter), encrypted_payload, None)
            )
            self.receive_counter += 1
            if len(result) >= _MAX_CHUNK:
                break
            if self.reader.at_eof():
                break
            # The relay consumes chunks opportunistically; one read is enough
            # for SOCKS CONNECT replies and avoids buffering the whole stream.
            break
        return bytes(result)


class ShadowsocksBridge:
    """Expose one Shadowsocks node as a temporary local SOCKS5 endpoint."""

    def __init__(self, node: ShadowsocksNode):
        self.node = node
        self.server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"socks5h://127.0.0.1:{port}"

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        remote: asyncio.StreamWriter | None = None
        connected = False
        try:
            version, methods_count = await reader.readexactly(2)
            if version != _SOCKS5_VERSION:
                raise ValueError("本地代理不是 SOCKS5 请求")
            await reader.readexactly(methods_count)
            writer.write(b"\x05\x00")
            await writer.drain()

            version, command, _, address_type = await reader.readexactly(4)
            if version != _SOCKS5_VERSION or command != 1:
                raise ValueError("Shadowsocks 桥接只支持 SOCKS5 CONNECT")
            host = await self._read_socks_address(reader, address_type)
            port = struct.unpack(">H", await reader.readexactly(2))[0]

            remote_reader, remote = await asyncio.open_connection(self.node.server, self.node.port)
            stream = _AeadStream(remote_reader, remote, self.node)
            await stream.send(_target_address(host, port))
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            connected = True

            async def upload() -> None:
                while True:
                    data = await reader.read(64 * 1024)
                    if not data:
                        break
                    await stream.send(data)

            async def download() -> None:
                while True:
                    data = await stream.receive()
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()

            tasks = {asyncio.create_task(upload()), asyncio.create_task(download())}
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            if not connected:
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                try:
                    await writer.drain()
                except Exception:
                    pass
        finally:
            writer.close()
            await writer.wait_closed()
            if remote is not None:
                remote.close()
                await remote.wait_closed()

    @staticmethod
    async def _read_socks_address(reader: asyncio.StreamReader, address_type: int) -> str:
        if address_type == 1:
            return str(ipaddress.IPv4Address(await reader.readexactly(4)))
        if address_type == 3:
            length = (await reader.readexactly(1))[0]
            return (await reader.readexactly(length)).decode("idna")
        if address_type == 4:
            return str(ipaddress.IPv6Address(await reader.readexactly(16)))
        raise ValueError("不支持的 SOCKS5 地址类型")


async def with_shadowsocks_proxy(
    proxy_url: str, callback: Callable[[str], Awaitable[object]]
) -> object:
    """Run ``callback`` with a local SOCKS5 URL when ``proxy_url`` is ss://."""
    if not proxy_url.lower().startswith("ss://"):
        return await callback(proxy_url)
    async with ShadowsocksBridge(parse_shadowsocks_url(proxy_url)) as local_proxy:
        return await callback(local_proxy)


@asynccontextmanager
async def shadowsocks_proxy_context(proxy_url: object) -> AsyncIterator[object]:
    """Yield a proxy URL suitable for httpx, bridging ``ss://`` when needed."""
    if not isinstance(proxy_url, str) or not proxy_url.lower().startswith("ss://"):
        yield proxy_url
        return
    async with ShadowsocksBridge(parse_shadowsocks_url(proxy_url)) as local_proxy:
        yield local_proxy

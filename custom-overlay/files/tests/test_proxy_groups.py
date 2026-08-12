import asyncio
import os
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import aiosqlite


class ProxyImportParsingTests(unittest.TestCase):
    def test_clash_shadowsocks_yaml_import_keeps_protocol_credentials_private(self):
        from src.proxy_groups import mask_proxy_url, parse_proxy_import
        from src.shadowsocks import parse_shadowsocks_url

        result = parse_proxy_import(
            """
            - name: "住宅号池2"
              type: ss
              server: aaaaa
              port: 31002
              cipher: aes-256-gcm
              password: "aaaaaaa"
              udp: true
            """
        )

        self.assertEqual(len(result), 1)
        node = parse_shadowsocks_url(result[0]["proxy_url"])
        self.assertEqual(node.name, "住宅号池2")
        self.assertEqual(node.server, "aaaaa")
        self.assertEqual(node.port, 31002)
        self.assertEqual(node.method, "aes-256-gcm")
        self.assertEqual(node.password, "aaaaaaa")
        self.assertNotIn("aaaaaaa", mask_proxy_url(result[0]["proxy_url"]))

    def test_shadowsocks_uri_import_is_supported(self):
        from src.proxy_groups import parse_proxy_import
        from src.shadowsocks import parse_shadowsocks_url, shadowsocks_uri

        uri = shadowsocks_uri(
            name="住宅号池2",
            server="aaaaa",
            port=31002,
            method="aes-256-gcm",
            password="aaaaaaa",
        )
        result = parse_proxy_import(uri)
        node = parse_shadowsocks_url(result[0]["proxy_url"])
        self.assertEqual(node.server, "aaaaa")
        self.assertEqual(node.port, 31002)
        self.assertEqual(node.method, "aes-256-gcm")

    def test_yaml_and_json_proxy_envelopes_are_supported(self):
        from src.proxy_groups import parse_proxy_import

        yaml_result = parse_proxy_import(
            """
            proxies:
              - name: office
                type: http
                server: proxy.example
                port: 8080
            """
        )
        json_result = parse_proxy_import(
            '{"proxies":[{"name":"secure","type":"socks5","server":"proxy.example","port":1080}]}'
        )

        self.assertEqual(yaml_result[0]["name"], "office")
        self.assertEqual(yaml_result[0]["proxy_url"], "http://proxy.example:8080")
        self.assertEqual(json_result[0]["name"], "secure")
        self.assertEqual(json_result[0]["proxy_url"], "socks5://proxy.example:1080")

    def test_proxy_import_accepts_common_text_formats(self):
        from src.proxy_groups import parse_proxy_import

        result = parse_proxy_import(
            """
            http://alice:secret@proxy-a.example:8080
            proxy-b.example:3128
            proxy-c.example:1080:bob:password
            office|socks5://proxy-d.example:1080
            """
        )

        self.assertEqual(
            [item["proxy_url"] for item in result],
            [
                "http://alice:secret@proxy-a.example:8080",
                "http://proxy-b.example:3128",
                "http://bob:password@proxy-c.example:1080",
                "socks5://proxy-d.example:1080",
            ],
        )
        self.assertEqual(result[-1]["name"], "office")

    def test_proxy_import_deduplicates_and_rejects_unsupported_schemes(self):
        from src.proxy_groups import parse_proxy_import

        result = parse_proxy_import(
            [
                "https://proxy.example:443",
                {"name": "duplicate", "url": "https://proxy.example:443"},
                {"host": "socks.example", "port": 1080, "scheme": "socks5"},
            ]
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["proxy_url"], "socks5://socks.example:1080")
        with self.assertRaises(ValueError):
            parse_proxy_import("vmess://unsupported")


class ShadowsocksBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_interoperates_with_aead_server_response_salt(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.hashes import SHA1
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        from src.shadowsocks import (
            ShadowsocksBridge,
            _evp_bytes_to_key,
            _nonce,
            parse_shadowsocks_url,
            shadowsocks_uri,
        )

        target_received = asyncio.get_running_loop().create_future()
        master_key = _evp_bytes_to_key(b"secret", 32)

        def cipher_for(salt):
            subkey = HKDF(
                algorithm=SHA1(), length=32, salt=salt, info=b"ss-subkey"
            ).derive(master_key)
            return AESGCM(subkey)

        async def fake_shadowsocks_server(reader, writer):
            try:
                request_cipher = cipher_for(await reader.readexactly(32))
                encrypted_length = await reader.readexactly(18)
                length = int.from_bytes(
                    request_cipher.decrypt(_nonce(0), encrypted_length, None), "big"
                )
                encrypted_payload = await reader.readexactly(length + 16)
                target_received.set_result(
                    request_cipher.decrypt(_nonce(1), encrypted_payload, None)
                )

                response_salt = b"s" * 32
                response_cipher = cipher_for(response_salt)
                response = b"ok"
                writer.write(
                    response_salt
                    + response_cipher.encrypt(_nonce(0), len(response).to_bytes(2, "big"), None)
                    + response_cipher.encrypt(_nonce(1), response, None)
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(fake_shadowsocks_server, "127.0.0.1", 0)
        server_port = server.sockets[0].getsockname()[1]
        uri = shadowsocks_uri(
            name="interop-test",
            server="127.0.0.1",
            port=server_port,
            method="aes-256-gcm",
            password="secret",
        )
        try:
            async with ShadowsocksBridge(parse_shadowsocks_url(uri)) as local_proxy:
                parsed = urlsplit(local_proxy)
                reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                self.assertEqual(await reader.readexactly(2), b"\x05\x00")

                host = b"example.com"
                writer.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + b"\x01\xbb")
                await writer.drain()
                self.assertEqual(await reader.readexactly(10), b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                self.assertEqual(await reader.readexactly(2), b"ok")
                self.assertEqual(
                    await target_received,
                    b"\x03" + bytes([len(host)]) + host + b"\x01\xbb",
                )
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_bridge_exposes_a_local_socks5_endpoint(self):
        from src.shadowsocks import ShadowsocksBridge, shadowsocks_uri

        uri = shadowsocks_uri(
            name="bridge-test",
            server="127.0.0.1",
            port=31002,
            method="aes-256-gcm",
            password="secret",
        )
        from src.shadowsocks import parse_shadowsocks_url

        async with ShadowsocksBridge(parse_shadowsocks_url(uri)) as local_proxy:
            parsed = urlsplit(local_proxy)
            self.assertEqual(parsed.scheme, "socks5h")
            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            self.assertEqual(await reader.readexactly(2), b"\x05\x00")
            writer.close()
            await writer.wait_closed()

    async def test_httpx_client_bridges_ss_proxy_without_external_connection(self):
        import httpx

        from src.httpx_client import HttpxClientManager
        from src.shadowsocks import shadowsocks_uri

        uri = shadowsocks_uri(
            name="httpx-test",
            server="127.0.0.1",
            port=31002,
            method="aes-256-gcm",
            password="secret",
        )
        manager = HttpxClientManager()
        async with manager.get_client(proxy_url=uri) as client:
            self.assertIsInstance(client, httpx.AsyncClient)


class ProxyGroupStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.storage.sqlite_manager import SQLiteManager

        self.tempdir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CREDENTIALS_DIR": self.tempdir.name}, clear=False)
        self.env.start()
        self.manager = SQLiteManager()
        await self.manager.initialize()

    async def asyncTearDown(self):
        await self.manager.close()
        self.env.stop()
        self.tempdir.cleanup()

    async def test_realtime_import_round_robin_and_batch_account_binding(self):
        group = await self.manager.create_proxy_group(
            name="生产代理",
            description="Antigravity accounts",
            strategy="round_robin",
        )
        imported = await self.manager.import_proxy_group_nodes(
            group["id"],
            [
                {"name": "node-a", "proxy_url": "http://proxy-a.example:8080"},
                {"name": "node-b", "proxy_url": "socks5://proxy-b.example:1080"},
            ],
        )
        self.assertEqual(imported["imported_count"], 2)

        await self.manager.store_credential("one.json", {"access_token": "one"}, mode="antigravity")
        await self.manager.store_credential("two.json", {"access_token": "two"}, mode="antigravity")
        result = await self.manager.batch_update_credential_network(
            ["one.json", "two.json"],
            proxy_mode="group",
            proxy_group_id=group["id"],
            mode="antigravity",
        )
        self.assertEqual(result["updated_count"], 2)
        self.assertEqual(result["missing"], [])

        first = await self.manager.resolve_credential_network("one.json", mode="antigravity")
        second = await self.manager.resolve_credential_network("two.json", mode="antigravity")
        self.assertEqual(first["proxy_url"], "http://proxy-a.example:8080")
        self.assertEqual(second["proxy_url"], "socks5://proxy-b.example:1080")
        self.assertEqual(first["proxy_group_id"], group["id"])
        self.assertEqual(second["proxy_group_id"], group["id"])

        groups = await self.manager.list_proxy_groups()
        self.assertEqual(groups[0]["assigned_account_count"], 2)
        self.assertEqual(groups[0]["node_count"], 2)

    async def test_group_proxy_binding_is_sticky_for_each_account(self):
        group = await self.manager.create_proxy_group(
            name="固定账号出口",
            strategy="round_robin",
        )
        await self.manager.import_proxy_group_nodes(
            group["id"],
            [
                {"name": "node-a", "proxy_url": "http://proxy-a.example:8080"},
                {"name": "node-b", "proxy_url": "http://proxy-b.example:8080"},
            ],
        )
        await self.manager.store_credential(
            "sticky.json",
            {"access_token": "token", "project_id": "project"},
            mode="antigravity",
        )
        await self.manager.update_credential_network(
            "sticky.json",
            proxy_mode="group",
            proxy_group_id=group["id"],
            mode="antigravity",
        )

        first = await self.manager.resolve_credential_network(
            "sticky.json", mode="antigravity"
        )
        second = await self.manager.resolve_credential_network(
            "sticky.json", mode="antigravity"
        )
        stored = await self.manager.get_credential_network(
            "sticky.json", mode="antigravity"
        )

        self.assertEqual(first["proxy_node_id"], second["proxy_node_id"])
        self.assertEqual(first["proxy_url"], second["proxy_url"])
        self.assertEqual(stored["bound_proxy_node_id"], first["proxy_node_id"])

    async def test_invalidating_group_binding_cools_old_node_and_rebinds(self):
        group = await self.manager.create_proxy_group(
            name="403重绑",
            strategy="round_robin",
        )
        await self.manager.import_proxy_group_nodes(
            group["id"],
            [
                {"name": "node-a", "proxy_url": "http://proxy-a.example:8080"},
                {"name": "node-b", "proxy_url": "http://proxy-b.example:8080"},
            ],
        )
        await self.manager.store_credential(
            "rebind.json",
            {"access_token": "token", "project_id": "project"},
            mode="antigravity",
        )
        await self.manager.update_credential_network(
            "rebind.json",
            proxy_mode="group",
            proxy_group_id=group["id"],
            mode="antigravity",
        )

        first = await self.manager.resolve_credential_network(
            "rebind.json", mode="antigravity"
        )
        invalidated = await self.manager.invalidate_antigravity_proxy_binding(
            "rebind.json", cooldown_seconds=300
        )
        second = await self.manager.resolve_credential_network(
            "rebind.json", mode="antigravity"
        )
        detail = await self.manager.get_proxy_group(group["id"])
        old_node = next(
            node for node in detail["nodes"] if node["id"] == first["proxy_node_id"]
        )

        self.assertTrue(invalidated)
        self.assertNotEqual(first["proxy_node_id"], second["proxy_node_id"])
        self.assertGreater(old_node["failure_count"], 0)
        self.assertIsNotNone(old_node["cooldown_until"])

    async def test_account_health_persists_egress_eligibility_and_403_state(self):
        await self.manager.store_credential(
            "health.json",
            {"access_token": "token", "project_id": "project"},
            mode="antigravity",
        )

        initial = await self.manager.get_antigravity_account_health("health.json")
        self.assertEqual(initial["binding_status"], "unchecked")
        self.assertEqual(initial["eligibility_status"], "unchecked")

        updated = await self.manager.update_antigravity_account_health(
            "health.json",
            egress_ip="203.0.113.10",
            egress_country="US",
            binding_status="healthy",
            eligibility_status="eligible",
            eligibility_reason="loadCodeAssist accepted",
            eligibility_checked_at=1234.5,
            last_403_category="geo_blocked",
            last_403_reason="location unavailable",
            last_403_at=1200.0,
        )
        reloaded = await self.manager.get_antigravity_account_health("health.json")

        self.assertEqual(updated, reloaded)
        self.assertEqual(reloaded["egress_ip"], "203.0.113.10")
        self.assertEqual(reloaded["binding_status"], "healthy")
        self.assertEqual(reloaded["eligibility_status"], "eligible")
        self.assertEqual(reloaded["last_403_category"], "geo_blocked")

        summary = await self.manager.get_credentials_summary(
            mode="antigravity", limit=20
        )
        item = next(
            row for row in summary["items"] if row["filename"] == "health.json"
        )
        self.assertEqual(item["egress_ip"], "203.0.113.10")
        self.assertEqual(item["egress_country"], "US")
        self.assertEqual(item["binding_status"], "healthy")
        self.assertEqual(item["eligibility_status"], "eligible")

    async def test_success_clears_stale_antigravity_403_health(self):
        await self.manager.store_credential(
            "recovered.json",
            {"access_token": "token", "project_id": "project"},
            mode="antigravity",
        )
        await self.manager.update_credential_state(
            "recovered.json",
            {
                "error_codes": [403],
                "error_messages": {"403": "Forbidden"},
            },
            mode="antigravity",
        )
        await self.manager.update_antigravity_account_health(
            "recovered.json",
            binding_status="healthy",
            eligibility_status="geo_blocked",
            eligibility_reason="stale location result",
            eligibility_checked_at=1000.0,
            last_403_category="geo_blocked",
            last_403_reason="Forbidden",
            last_403_at=1000.0,
        )

        await self.manager.record_success(
            "recovered.json",
            model_name="gemini-2.5-flash",
            mode="antigravity",
        )

        errors = await self.manager.get_credential_errors(
            "recovered.json", mode="antigravity"
        )
        health = await self.manager.get_antigravity_account_health(
            "recovered.json"
        )
        self.assertEqual(errors["error_codes"], [])
        self.assertEqual(errors["error_messages"], {})
        self.assertEqual(health["binding_status"], "healthy")
        self.assertEqual(health["eligibility_status"], "eligible")
        self.assertIsNone(health["eligibility_reason"])
        self.assertIsNone(health["last_403_category"])
        self.assertIsNone(health["last_403_reason"])
        self.assertIsNone(health["last_403_at"])

    async def test_concurrent_success_writes_use_only_one_sqlite_connection(self):
        from src.storage import sqlite_manager as sqlite_module

        await self.manager.store_credential(
            "concurrent.json",
            {"access_token": "token", "project_id": "project"},
            mode="antigravity",
        )
        original_connect = sqlite_module.aiosqlite.connect
        active_connections = 0
        peak_connections = 0

        class TrackedConnection:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                nonlocal active_connections, peak_connections
                opened = await self.connection.__aenter__()
                active_connections += 1
                peak_connections = max(peak_connections, active_connections)
                await asyncio.sleep(0)
                return opened

            async def __aexit__(self, exc_type, exc, traceback):
                nonlocal active_connections
                try:
                    return await self.connection.__aexit__(
                        exc_type, exc, traceback
                    )
                finally:
                    active_connections -= 1

        def tracked_connect(*args, **kwargs):
            return TrackedConnection(original_connect(*args, **kwargs))

        with patch.object(sqlite_module.aiosqlite, "connect", tracked_connect):
            await asyncio.gather(*(
                self.manager.record_success(
                    "concurrent.json",
                    model_name="gemini-2.5-flash",
                    mode="antigravity",
                )
                for _ in range(20)
            ))

        self.assertEqual(peak_connections, 1)

    async def test_replace_import_is_immediately_visible(self):
        group = await self.manager.create_proxy_group(name="实时组")
        await self.manager.import_proxy_group_nodes(
            group["id"],
            [{"proxy_url": "http://old.example:8080"}],
        )
        await self.manager.import_proxy_group_nodes(
            group["id"],
            [{"proxy_url": "http://new.example:8080"}],
            replace=True,
        )

        detail = await self.manager.get_proxy_group(group["id"])
        self.assertEqual(
            [node["proxy_url"] for node in detail["nodes"]],
            ["http://new.example:8080"],
        )

    async def test_account_bound_to_empty_group_is_not_selected_for_requests(self):
        group = await self.manager.create_proxy_group(name="空代理组")
        await self.manager.store_credential(
            "empty.json",
            {"access_token": "token", "project_id": "project"},
            mode="antigravity",
        )
        await self.manager.update_credential_network(
            "empty.json",
            proxy_mode="group",
            proxy_group_id=group["id"],
            mode="antigravity",
        )

        selected = await self.manager.get_next_available_credential(mode="antigravity")
        self.assertIsNone(selected)

    async def test_credential_selection_can_exclude_already_attempted_accounts(self):
        await self.manager.store_credential(
            "skip.json",
            {"access_token": "skip", "project_id": "project"},
            mode="antigravity",
        )
        await self.manager.store_credential(
            "keep.json",
            {"access_token": "keep", "project_id": "project"},
            mode="antigravity",
        )

        selected = await self.manager.get_next_available_credential(
            mode="antigravity", exclude_filenames=["skip.json"]
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], "keep.json")

    async def test_batch_update_is_atomic_when_an_account_is_missing(self):
        group = await self.manager.create_proxy_group(name="原子切换组")
        await self.manager.store_credential(
            "exists.json", {"access_token": "token"}, mode="antigravity"
        )

        result = await self.manager.batch_update_credential_network(
            ["exists.json", "missing.json"],
            proxy_mode="group",
            proxy_group_id=group["id"],
            mode="antigravity",
        )

        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["missing"], ["missing.json"])
        network = await self.manager.get_credential_network(
            "exists.json", mode="antigravity"
        )
        self.assertEqual(network["proxy_mode"], "inherit")

    async def test_deleting_an_unused_group_removes_its_nodes(self):
        group = await self.manager.create_proxy_group(name="可删除组")
        await self.manager.import_proxy_group_nodes(
            group["id"], [{"proxy_url": "http://delete.example:8080"}]
        )

        self.assertTrue(await self.manager.delete_proxy_group(group["id"]))
        async with aiosqlite.connect(self.manager._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM proxy_group_nodes WHERE group_id = ?",
                (group["id"],),
            ) as cursor:
                self.assertEqual((await cursor.fetchone())[0], 0)


if __name__ == "__main__":
    unittest.main()

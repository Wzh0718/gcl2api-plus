import asyncio
import unittest


class _BlockingSuccessBackend:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def record_success(self, filename, model_name=None, mode="geminicli"):
        self.started.set()
        await self.release.wait()


class _FakeStorageAdapter:
    def __init__(self, backend):
        self._backend = backend


class CredentialSuccessPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_singleton_proxy_supports_synchronous_manager_methods(self):
        from src.credential_manager import _CredentialManagerSingleton

        class FakeManager:
            def __init__(self):
                self.calls = []

            def record_image_account_outcome(self, credential_name, **kwargs):
                self.calls.append((credential_name, kwargs))
                return None

        manager = FakeManager()
        singleton = _CredentialManagerSingleton()
        singleton._instance = manager

        result = await singleton.record_image_account_outcome(
            "account.json",
            success=True,
            latency_seconds=1.25,
        )

        self.assertIsNone(result)
        self.assertEqual(
            manager.calls,
            [
                (
                    "account.json",
                    {"success": True, "latency_seconds": 1.25},
                )
            ],
        )

    async def test_success_persistence_is_awaited_instead_of_fire_and_forget(self):
        from src.credential_manager import CredentialManager

        backend = _BlockingSuccessBackend()
        manager = CredentialManager()
        manager._initialized = True
        manager._storage_adapter = _FakeStorageAdapter(backend)

        call = asyncio.create_task(
            manager.record_api_call_result(
                "account.json",
                True,
                mode="antigravity",
                model_name="gemini-2.5-flash",
            )
        )
        await backend.started.wait()

        self.assertFalse(call.done())
        backend.release.set()
        await call

    async def test_successful_antigravity_probe_reenables_credential(self):
        from src.panel import creds

        class FakeCredentialManager:
            def __init__(self):
                self.recorded = []
                self.disabled = []

            async def record_api_call_result(self, *args, **kwargs):
                self.recorded.append((args, kwargs))

            async def set_cred_disabled(self, *args, **kwargs):
                self.disabled.append((args, kwargs))

        manager = FakeCredentialManager()
        original = creds.credential_manager
        creds.credential_manager = manager
        try:
            await creds._recover_credential_after_success(
                "account.json",
                mode="antigravity",
                model_name="gemini-2.5-flash",
            )
        finally:
            creds.credential_manager = original

        self.assertEqual(len(manager.recorded), 1)
        self.assertEqual(
            manager.disabled,
            [(('account.json', False), {'mode': 'antigravity'})],
        )

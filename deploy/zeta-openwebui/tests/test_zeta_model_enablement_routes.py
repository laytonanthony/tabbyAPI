import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


backend = os.environ.get("ZETA_OPENWEBUI_BACKEND")
if backend:
    sys.path.insert(0, str(Path(backend).resolve()))

try:
    from fastapi import HTTPException
    from open_webui.routers import openai as openai_module
except ModuleNotFoundError as exc:
    if exc.name == "open_webui":
        raise unittest.SkipTest(
            "Set ZETA_OPENWEBUI_BACKEND to run OpenWebUI route integration tests"
        ) from None
    raise

if not hasattr(openai_module, "_require_registered_model_enabled"):
    raise unittest.SkipTest("Install the current Zeta OpenWebUI overlay first")


def registry_row(model_id, *, active=True, base_model_id=None):
    return SimpleNamespace(
        id=model_id,
        is_active=active,
        base_model_id=base_model_id,
    )


class TestOpenAIModelListEnablement(unittest.IsolatedAsyncioTestCase):
    async def test_admin_model_list_omits_disabled_and_keeps_raw_cache_untouched(self):
        discovered = {
            "object": "list",
            "data": [
                {"id": "paid", "urlIdx": 1},
                {"id": "local", "urlIdx": 0},
            ],
        }
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    config=SimpleNamespace(ENABLE_OPENAI_API=True),
                )
            )
        )

        with patch.object(
            openai_module,
            "get_all_models",
            new=AsyncMock(return_value=discovered),
        ), patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[
                registry_row("paid", active=False),
                registry_row("local", active=True),
            ],
        ):
            result = await openai_module.get_models(
                request,
                url_idx=None,
                user=SimpleNamespace(id="admin", role="admin"),
            )

        self.assertEqual([item["id"] for item in result["data"]], ["local"])
        self.assertEqual(
            [item["id"] for item in discovered["data"]],
            ["paid", "local"],
        )


class TestResponsesModelEnablement(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_model_is_rejected_for_admin_before_outbound_session(self):
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    OPENAI_MODELS={"paid": {"id": "paid", "urlIdx": 0}},
                )
            )
        )

        with patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[registry_row("paid", active=False)],
        ), patch.object(openai_module.aiohttp, "ClientSession") as session:
            with self.assertRaises(HTTPException) as context:
                await openai_module._proxy_responses_request(
                    request,
                    {"model": "paid", "input": "hello"},
                    SimpleNamespace(id="admin", role="admin"),
                )

        self.assertEqual(context.exception.status_code, 404)
        self.assertEqual(context.exception.detail, "Model not found")
        session.assert_not_called()

    async def test_active_preset_cannot_route_to_disabled_base(self):
        with patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[
                registry_row("paid", active=False),
                registry_row("preset", active=True, base_model_id="paid"),
            ],
        ):
            with self.assertRaises(HTTPException) as context:
                openai_module._require_registered_model_enabled("preset")

        self.assertEqual(context.exception.status_code, 404)

    async def test_active_or_reenabled_model_passes_enablement_gate(self):
        with patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[registry_row("local", active=True)],
        ):
            openai_module._require_registered_model_enabled("local")

    async def test_normal_user_acl_accepts_list_and_rejects_empty_list(self):
        user = SimpleNamespace(id="user-a", role="user")
        with patch.object(
            openai_module,
            "_require_registered_model_enabled",
        ), patch.object(
            openai_module,
            "get_filtered_models",
            new=AsyncMock(return_value=[]),
        ):
            with self.assertRaises(HTTPException) as denied:
                await openai_module._require_responses_model_access(
                    {"id": "local"}, user
                )
        self.assertEqual(denied.exception.status_code, 403)

        with patch.object(
            openai_module,
            "_require_registered_model_enabled",
        ), patch.object(
            openai_module,
            "get_filtered_models",
            new=AsyncMock(return_value=[{"id": "local"}]),
        ):
            await openai_module._require_responses_model_access(
                {"id": "local"}, user
            )

    async def test_registry_failure_is_503_not_fail_open(self):
        class BrokenDatabase:
            def __enter__(self):
                raise RuntimeError("database URL and credentials must not leak")

            def __exit__(self, *_args):
                return False

        with patch.object(openai_module, "get_db", return_value=BrokenDatabase()):
            with self.assertRaises(HTTPException) as context:
                openai_module._require_registered_model_enabled("paid")

        self.assertEqual(context.exception.status_code, 503)
        self.assertEqual(context.exception.detail, "Unable to verify model availability")
        self.assertNotIn("credentials", context.exception.detail)


class TestChatModelEnablement(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_chat_model_is_rejected_before_provider_or_model_lookup(self):
        request = SimpleNamespace(state=SimpleNamespace(bypass_filter=False))
        with patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[registry_row("paid", active=False)],
        ), patch.object(openai_module.Models, "get_model_by_id") as model_lookup, patch.object(
            openai_module.aiohttp, "ClientSession"
        ) as session:
            with self.assertRaises(HTTPException) as context:
                await openai_module.generate_chat_completion(
                    request,
                    {"model": "paid", "messages": []},
                    user=SimpleNamespace(id="admin", role="admin"),
                )

        self.assertEqual(context.exception.status_code, 404)
        model_lookup.assert_not_called()
        session.assert_not_called()


class TestAdditionalExecutionGates(unittest.IsolatedAsyncioTestCase):
    async def test_generic_json_proxy_rejects_disabled_model_before_outbound(self):
        request = SimpleNamespace(
            body=AsyncMock(return_value=b'{"model":"paid","input":"hello"}'),
            app=SimpleNamespace(state=SimpleNamespace(OPENAI_MODELS={})),
        )
        with patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[registry_row("paid", active=False)],
        ), patch.object(openai_module.aiohttp, "ClientSession") as session:
            with self.assertRaises(HTTPException) as context:
                await openai_module.proxy(
                    "batches",
                    request,
                    user=SimpleNamespace(id="admin", role="admin"),
                )

        self.assertEqual(context.exception.status_code, 404)
        session.assert_not_called()

    async def test_speech_json_rejects_disabled_model_before_paid_request(self):
        request = SimpleNamespace(
            body=AsyncMock(return_value=b'{"model":"tts-paid","input":"hello"}'),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    config=SimpleNamespace(
                        OPENAI_API_BASE_URLS=["https://api.openai.com/v1"]
                    )
                )
            ),
        )
        with patch.object(
            openai_module,
            "_registered_model_enablement_rows",
            return_value=[registry_row("tts-paid", active=False)],
        ), patch.object(openai_module.requests, "post") as post:
            with self.assertRaises(HTTPException) as context:
                await openai_module.speech(
                    request,
                    user=SimpleNamespace(id="admin", role="admin"),
                )

        self.assertEqual(context.exception.status_code, 404)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

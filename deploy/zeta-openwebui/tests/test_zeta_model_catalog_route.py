import json
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
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from open_webui.routers import zeta_model_catalog as route_module
    from open_webui.utils.auth import get_verified_user
except ModuleNotFoundError as exc:
    if exc.name == "open_webui":
        raise unittest.SkipTest(
            "Set ZETA_OPENWEBUI_BACKEND to run OpenWebUI route integration tests"
        ) from None
    raise


def registry_model(model_id, owner, *, enabled=True, priority=10, active=True):
    return SimpleNamespace(
        id=model_id,
        name=model_id.replace("-", " ").title(),
        user_id=owner,
        base_model_id=None,
        is_active=active,
        meta={
            "zeta_catalog": {
                "enabled": enabled,
                "priority": priority,
                "context_window": 32_768,
            }
        },
    )


class TestCatalogueRoute(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(route_module.router, prefix="/api/v1/models")
        self.app.state.config = SimpleNamespace(MODEL_ORDER_LIST=[])
        self.users = {
            "key-a": SimpleNamespace(
                id="user-a",
                role="user",
            ),
            "key-b": SimpleNamespace(
                id="user-b",
                role="user",
            ),
            "key-admin": SimpleNamespace(
                id="admin-a",
                role="admin",
            ),
        }

        async def authenticate(request: Request):
            header = request.headers.get("authorization", "")
            user = self.users.get(header.removeprefix("Bearer "))
            if user is None:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            return user

        self.app.dependency_overrides[get_verified_user] = authenticate
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    @patch.object(route_module.openai, "get_all_models", new_callable=AsyncMock)
    @patch.object(route_module.AccessGrants, "get_accessible_resource_ids")
    @patch.object(route_module.Groups, "get_groups_by_member_id", return_value=[])
    @patch.object(route_module.Models, "get_all_models")
    def test_user_api_keys_are_filtered_to_owner_or_read_grant(
        self, get_models, _groups, get_grants, discovery
    ):
        get_models.return_value = [
            registry_model("model-a", "user-a", priority=20),
            registry_model("shared", "owner", priority=10),
            registry_model("model-b", "user-b", priority=30),
        ]
        discovery.return_value = {"data": []}
        get_grants.side_effect = lambda **kwargs: (
            {"shared"} if kwargs["user_id"] == "user-a" else set()
        )

        with patch.object(route_module, "BYPASS_MODEL_ACCESS_CONTROL", False):
            response_a = self.client.get(
                "/api/v1/models/catalog", headers={"Authorization": "Bearer key-a"}
            )
            response_b = self.client.get(
                "/api/v1/models/catalog", headers={"Authorization": "Bearer key-b"}
            )

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response_a.json()["models"]],
            ["shared", "model-a"],
        )
        self.assertEqual(
            [item["id"] for item in response_b.json()["models"]],
            ["model-b"],
        )
        self.assertNotIn("user-a", response_a.text)
        self.assertNotIn("owner", response_a.text)

    def test_authentication_happens_before_etag(self):
        response = self.client.get(
            "/api/v1/models/catalog", headers={"If-None-Match": "*"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Invalid credentials"})

    @patch.object(route_module.openai, "get_all_models", new_callable=AsyncMock)
    @patch.object(route_module.Models, "get_all_models", return_value=[])
    def test_admin_receives_safe_unregistered_live_responses_model(
        self, _get_models, discovery
    ):
        discovery.return_value = {
            "data": [
                {"id": "new-provider-model", "urlIdx": 4},
                {"id": "http://127.0.0.1/internal", "urlIdx": 5},
            ]
        }

        response = self.client.get(
            "/api/v1/models/catalog",
            headers={"Authorization": "Bearer key-admin"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["models"]],
            ["new-provider-model"],
        )
        self.assertNotIn("urlIdx", response.text)

    @patch.object(route_module.openai, "get_all_models", new_callable=AsyncMock)
    @patch.object(route_module.AccessGrants, "get_accessible_resource_ids")
    @patch.object(route_module.Groups, "get_groups_by_member_id", return_value=[])
    @patch.object(route_module.Models, "get_all_models")
    def test_live_inactive_registry_row_is_absent_for_authorised_user(
        self, get_models, _groups, get_grants, discovery
    ):
        get_models.return_value = [
            registry_model("live-inactive", "owner", enabled=False, active=False)
        ]
        discovery.return_value = {"data": [{"id": "live-inactive"}]}
        get_grants.return_value = {"live-inactive"}

        with patch.object(route_module, "BYPASS_MODEL_ACCESS_CONTROL", False):
            response = self.client.get(
                "/api/v1/models/catalog",
                headers={"Authorization": "Bearer key-a"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], [])
        get_models.assert_called_once()

    @patch.object(route_module.openai, "get_all_models", new_callable=AsyncMock)
    @patch.object(route_module.AccessGrants, "get_accessible_resource_ids")
    @patch.object(route_module.Groups, "get_groups_by_member_id", return_value=[])
    @patch.object(route_module.Models, "get_all_models")
    def test_active_live_preset_cannot_bypass_disabled_base(
        self, get_models, _groups, get_grants, discovery
    ):
        base = registry_model("paid-base", "owner", active=False)
        preset = registry_model("paid-preset", "owner", active=True)
        preset.base_model_id = "paid-base"
        get_models.return_value = [base, preset]
        discovery.return_value = {
            "data": [{"id": "paid-base"}, {"id": "paid-preset"}]
        }
        get_grants.return_value = {"paid-base", "paid-preset"}

        with patch.object(route_module, "BYPASS_MODEL_ACCESS_CONTROL", False):
            response = self.client.get(
                "/api/v1/models/catalog",
                headers={"Authorization": "Bearer key-a"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], [])
        get_models.assert_called_once()

    @patch.object(route_module, "catalogue_for_user", new_callable=AsyncMock)
    def test_etag_returns_empty_304_and_private_cache_headers(self, catalogue_for_user):
        catalogue_for_user.return_value = [
            {
                "id": "model-a",
                "display_name": "Model A",
                "description": None,
                "priority": 10,
                "visibility": "list",
                "context_window": 32_768,
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": ["medium"],
                "input_modalities": ["text"],
                "capabilities": {
                    "tools": False,
                    "parallel_tools": False,
                    "images": False,
                    "web_search": False,
                    "reasoning": False,
                },
            }
        ]

        first = self.client.get(
            "/api/v1/models/catalog", headers={"Authorization": "Bearer key-a"}
        )
        second = self.client.get(
            "/api/v1/models/catalog",
            headers={
                "Authorization": "Bearer key-a",
                "If-None-Match": first.headers["etag"],
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")
        self.assertEqual(second.headers["etag"], first.headers["etag"])
        self.assertIn("private", second.headers["cache-control"])
        self.assertEqual(second.headers["vary"], "Authorization, Cookie")

    @patch.object(route_module, "catalogue_for_user", new_callable=AsyncMock)
    def test_server_failure_is_generic_json(self, catalogue_for_user):
        catalogue_for_user.side_effect = RuntimeError(
            "provider http://127.0.0.1 key sk-do-not-leak"
        )

        response = self.client.get(
            "/api/v1/models/catalog", headers={"Authorization": "Bearer key-a"}
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Unable to build model catalogue"})
        self.assertNotIn("127.0.0.1", response.text)
        self.assertNotIn("sk-do-not-leak", response.text)

    @patch.object(route_module, "catalogue_for_user", new_callable=AsyncMock)
    def test_internal_http_failure_is_also_generic(self, catalogue_for_user):
        catalogue_for_user.side_effect = HTTPException(
            status_code=502,
            detail="provider http://127.0.0.1 key sk-do-not-leak",
        )

        response = self.client.get(
            "/api/v1/models/catalog", headers={"Authorization": "Bearer key-a"}
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Unable to build model catalogue"})


if __name__ == "__main__":
    unittest.main()

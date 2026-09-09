import copy
import importlib.util
import json
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "zeta-openwebui"
    / "files"
    / "open_webui"
    / "utils"
    / "zeta_model_catalog.py"
)
SPEC = importlib.util.spec_from_file_location("zeta_model_catalog_under_test", MODULE_PATH)
catalog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(catalog)


def model(
    model_id,
    *,
    name=None,
    owner="owner-a",
    active=True,
    base_model_id=None,
    config=None,
    meta=None,
):
    metadata = dict(meta or {})
    if config is not None:
        metadata["zeta_catalog"] = config
    return {
        "id": model_id,
        "name": name or model_id,
        "user_id": owner,
        "is_active": active,
        "base_model_id": base_model_id,
        "meta": metadata,
    }


class TestCatalogueMembership(unittest.TestCase):
    def test_multiple_models_have_stable_priority_then_name_order(self):
        records = [
            model("beta", name="Beta", config={"enabled": True, "priority": 20}),
            model("zulu", name="Zulu", config={"enabled": True, "priority": 10}),
            model("alpha", name="Alpha", config={"enabled": True, "priority": 10}),
        ]

        result = catalog.build_catalogue_models(records)

        self.assertEqual([item["id"] for item in result], ["alpha", "zulu", "beta"])

    def test_explicit_model_remains_in_catalogue_while_offline(self):
        offline = model("offline-model", config={"enabled": True})

        self.assertTrue(catalog.is_catalogue_member(offline, live_model_ids=[]))
        self.assertEqual(
            catalog.build_catalogue_models([offline], live_model_ids=[])[0]["id"],
            "offline-model",
        )

    def test_live_registered_model_is_discovered_without_optional_metadata(self):
        live = model("new-live-model")

        result = catalog.build_catalogue_models(
            [live], live_model_ids=["new-live-model"]
        )

        self.assertEqual([item["id"] for item in result], ["new-live-model"])
        self.assertEqual(result[0]["context_window"], 32_768)

    def test_live_id_matching_is_exactly_case_sensitive(self):
        record = model("exact-ID")

        self.assertEqual(
            catalog.build_catalogue_models([record], live_model_ids=["exact-id"]),
            [],
        )

    def test_offline_opt_out_inactive_and_presets_are_excluded(self):
        records = [
            model("disabled", config={"enabled": False}),
            model("inactive", active=False, config={"enabled": True}),
            model("preset", base_model_id="base", config={"enabled": True}),
        ]

        self.assertEqual(
            catalog.build_catalogue_models(records, live_model_ids=[]),
            [],
        )

    def test_exact_live_models_still_require_enabled_registry_rows(self):
        records = [
            model("disabled", config={"enabled": False}),
            model("inactive", active=False, config={"enabled": False}),
            model("preset", base_model_id="base", config={"enabled": False}),
        ]

        result = catalog.build_catalogue_models(
            records, live_model_ids=["disabled", "inactive", "preset"]
        )

        self.assertEqual([item["id"] for item in result], ["disabled", "preset"])

    def test_active_preset_cannot_bypass_disabled_registered_base(self):
        records = [
            model("paid-base", active=False, config={"enabled": True}),
            model(
                "friendly-preset",
                active=True,
                base_model_id="paid-base",
                config={"enabled": True},
            ),
        ]

        result = catalog.build_catalogue_models(
            records,
            live_model_ids=["paid-base", "friendly-preset"],
        )

        self.assertEqual(result, [])

    def test_unregistered_provider_base_does_not_disable_active_preset(self):
        preset = model(
            "friendly-preset",
            active=True,
            base_model_id="provider-only-id",
        )

        result = catalog.build_catalogue_models(
            [preset], live_model_ids=["friendly-preset"]
        )

        self.assertEqual([item["id"] for item in result], ["friendly-preset"])

    def test_malformed_base_cycle_is_conservatively_unavailable(self):
        records = [
            model("cycle-a", base_model_id="cycle-b"),
            model("cycle-b", base_model_id="cycle-a"),
        ]

        self.assertEqual(
            catalog.unavailable_registered_model_ids(records),
            {"cycle-a", "cycle-b"},
        )

    def test_unsafe_internal_ids_are_excluded(self):
        records = [
            model("/media/models/private", config={"enabled": True}),
            model("http://127.0.0.1:8000/model", config={"enabled": True}),
            model("C:/models/private", config={"enabled": True}),
            model("safe/../../etc/passwd", config={"enabled": True}),
            model("safe//private", config={"enabled": True}),
            model("sk-supersecret123", config={"enabled": True}),
            model("safe/provider:model", config={"enabled": True}),
        ]

        result = catalog.build_catalogue_models(records)

        self.assertEqual([item["id"] for item in result], ["safe/provider:model"])

    def test_long_namespaced_provider_id_is_not_treated_as_a_secret(self):
        model_id = "organisation/" + ("a" * 96)

        result = catalog.build_catalogue_models(
            [model(model_id, config={"enabled": True})]
        )

        self.assertEqual([item["id"] for item in result], [model_id])


class TestCatalogueMetadata(unittest.TestCase):
    def test_complete_valid_metadata_is_preserved(self):
        record = model(
            "qwen",
            name="fallback",
            config={
                "enabled": True,
                "display_name": "Qwen 3.8 Flash",
                "description": "User-facing description",
                "priority": 0,
                "visibility": "hidden",
                "context_window": 262_144,
                "default_reasoning_level": "high",
                "supported_reasoning_levels": ["xhigh", "low", "high"],
                "input_modalities": ["image", "text"],
                "capabilities": {
                    "tools": True,
                    "parallel_tools": True,
                    "images": True,
                    "web_search": False,
                    "reasoning": True,
                },
            },
        )

        result = catalog.build_catalogue_models([record])[0]

        self.assertEqual(result["display_name"], "Qwen 3.8 Flash")
        self.assertEqual(result["priority"], 0)
        self.assertEqual(result["visibility"], "hidden")
        self.assertEqual(result["context_window"], 262_144)
        self.assertEqual(
            result["supported_reasoning_levels"], ["low", "high", "xhigh"]
        )
        self.assertEqual(result["default_reasoning_level"], "high")
        self.assertEqual(result["input_modalities"], ["text", "image"])
        self.assertTrue(result["capabilities"]["parallel_tools"])

    def test_missing_optional_metadata_uses_conservative_defaults(self):
        result = catalog.build_catalogue_models(
            [model("plain", config={"enabled": True})]
        )[0]

        self.assertEqual(result["description"], None)
        self.assertEqual(result["priority"], 10_000)
        self.assertEqual(result["visibility"], "list")
        self.assertEqual(result["context_window"], 32_768)
        self.assertEqual(result["default_reasoning_level"], "medium")
        self.assertEqual(result["supported_reasoning_levels"], ["medium"])
        self.assertEqual(result["input_modalities"], ["text"])
        self.assertEqual(result["capabilities"], {name: False for name in catalog.CAPABILITY_NAMES})

    def test_invalid_metadata_falls_back_without_coercion(self):
        record = model(
            "invalid",
            meta={"context": "262K upto 1M", "description": "http://127.0.0.1/admin"},
            config={
                "enabled": True,
                "priority": "1",
                "visibility": "public",
                "context_window": -5,
                "default_reasoning_level": "maximum",
                "supported_reasoning_levels": ["maximum", 1],
                "input_modalities": ["video", 1],
                "capabilities": {
                    "tools": 1,
                    "images": "yes",
                    "reasoning": True,
                },
            },
        )

        result = catalog.build_catalogue_models([record])[0]

        self.assertEqual(result["priority"], 10_000)
        self.assertEqual(result["visibility"], "list")
        self.assertEqual(result["context_window"], 32_768)
        self.assertEqual(result["supported_reasoning_levels"], ["medium"])
        self.assertEqual(result["input_modalities"], ["text"])
        self.assertFalse(result["capabilities"]["tools"])
        self.assertFalse(result["capabilities"]["images"])
        self.assertTrue(result["capabilities"]["reasoning"])
        self.assertIsNone(result["description"])

    def test_literal_credential_patterns_are_not_returned(self):
        for description in (
            "Connect with sk-supersecret123",
            "Authorization Bearer abcdefghijklmnop",
            "Use AIza0123456789abcdefghijklmnop",
            "Token ghp_abcdefghijklmnop",
            "JWT eyJabcdefghijk.abcdefghijklmnop.qrstuvwxyz12",
            "Backend backend.private.internal:8000",
            "IPv6 [::1]:8000",
            "Private IPv6 [fd00::2]:8000",
            "Link local IPv6 [fe80::abcd]:8000",
            "Private IPv6 fd00::2",
            "Link local IPv6 fe80::abcd",
            "Metadata 169.254.169.254/latest",
            "Shared address 100.64.0.1",
            "Benchmark address 198.18.0.1",
            "Public endpoint 8.8.8.8",
            "Socket unix:///var/run/model.sock",
            "File file:///etc/passwd",
            "Database postgresql://db.example/prod",
            "Path path=/home/anthony/private",
        ):
            with self.subTest(description=description):
                result = catalog.build_catalogue_models(
                    [
                        model(
                            "safe",
                            config={"enabled": True, "description": description},
                        )
                    ]
                )[0]
                self.assertIsNone(result["description"])

    def test_simple_legacy_context_is_derived_but_malformed_context_is_not(self):
        simple = model("simple", config={"enabled": True}, meta={"context": "128K"})
        malformed = model(
            "malformed", config={"enabled": True}, meta={"context": "128K["}
        )

        result = {
            item["id"]: item for item in catalog.build_catalogue_models([simple, malformed])
        }

        self.assertEqual(result["simple"]["context_window"], 131_072)
        self.assertEqual(result["malformed"]["context_window"], 32_768)

    def test_source_records_are_not_mutated(self):
        record = model("immutable", config={"enabled": True})
        before = copy.deepcopy(record)

        catalog.build_catalogue_models([record], live_model_ids=["immutable"])

        self.assertEqual(record, before)


class TestCatalogueAccess(unittest.TestCase):
    def setUp(self):
        self.records = [
            model("mine", owner="user-a", config={"enabled": True}),
            model("granted", owner="user-b", config={"enabled": True}),
            model("private", owner="user-b", config={"enabled": True}),
        ]

    def test_user_and_api_key_owner_only_receive_owned_or_granted_models(self):
        result = catalog.filter_authorised_models(
            self.records,
            user_id="user-a",
            user_role="user",
            bypass_model_access_control=False,
            granted_model_ids={"granted"},
        )

        self.assertEqual([item["id"] for item in result], ["mine", "granted"])

    def test_another_users_api_key_cannot_see_private_model(self):
        result = catalog.filter_authorised_models(
            self.records,
            user_id="user-c",
            user_role="user",
            bypass_model_access_control=False,
            granted_model_ids=set(),
        )

        self.assertEqual(result, [])

    def test_admin_and_configured_bypass_match_responses_access(self):
        admin = catalog.filter_authorised_models(
            self.records,
            user_id="admin",
            user_role="admin",
            bypass_model_access_control=False,
        )
        bypassed = catalog.filter_authorised_models(
            self.records,
            user_id="user-c",
            user_role="user",
            bypass_model_access_control=True,
        )

        self.assertEqual(admin, self.records)
        self.assertEqual(bypassed, self.records)


class TestCatalogueRevisionAndCaching(unittest.TestCase):
    def setUp(self):
        self.models = catalog.build_catalogue_models(
            [model("stable", config={"enabled": True, "priority": 5})]
        )

    def test_revision_is_stable_for_equivalent_content(self):
        shuffled = [{**self.models[0], "capabilities": dict(reversed(list(self.models[0]["capabilities"].items())))}]

        self.assertEqual(
            catalog.catalogue_revision(self.models),
            catalog.catalogue_revision(shuffled),
        )

    def test_revision_changes_with_catalogue_content_not_availability(self):
        record = model("stable", config={"enabled": True, "priority": 5})
        offline = catalog.build_catalogue_models([record], live_model_ids=[])
        online = catalog.build_catalogue_models([record], live_model_ids=["stable"])
        changed = copy.deepcopy(online)
        changed[0]["display_name"] = "Changed"

        self.assertEqual(catalog.catalogue_revision(offline), catalog.catalogue_revision(online))
        self.assertNotEqual(catalog.catalogue_revision(online), catalog.catalogue_revision(changed))

    def test_etag_supports_weak_strong_lists_and_wildcard(self):
        revision = catalog.catalogue_revision(self.models)

        self.assertTrue(catalog.if_none_match_matches(f'W/"{revision}"', revision))
        self.assertTrue(catalog.if_none_match_matches(f'"other", "{revision}"', revision))
        self.assertTrue(catalog.if_none_match_matches("*", revision))
        self.assertFalse(catalog.if_none_match_matches('W/"other"', revision))
        self.assertEqual(catalog.catalogue_etag(revision), f'W/"{revision}"')

    def test_cache_headers_are_private_and_vary_by_credentials(self):
        headers = catalog.response_headers("revision")

        self.assertIn("private", headers["Cache-Control"])
        self.assertIn("max-age=5", headers["Cache-Control"])
        self.assertEqual(headers["Vary"], "Authorization, Cookie")

    def test_public_json_cannot_expose_internal_fields_or_fixture_secrets(self):
        record = model(
            "safe-model",
            config={"enabled": True, "description": "safe description"},
        )
        record.update(
            {
                "api_key": "sk-secret-fixture",
                "base_url": "http://127.0.0.1:8000/v1",
                "system_prompt": "private prompt",
                "params": {"token": "private-token"},
                "access_grants": [{"principal_id": "other-user"}],
            }
        )

        payload = json.dumps(catalog.build_catalogue_models([record]), sort_keys=True)

        for forbidden in (
            "sk-secret-fixture",
            "127.0.0.1",
            "private prompt",
            "private-token",
            "other-user",
            "base_url",
            "access_grants",
        ):
            self.assertNotIn(forbidden, payload)


class TestCatalogueStatusMerge(unittest.TestCase):
    def test_existing_status_is_unchanged_and_offline_models_are_appended(self):
        existing = [
            {
                "id": "online",
                "state": "online",
                "online": True,
                "loaded": True,
                "source": "local",
            }
        ]
        before = copy.deepcopy(existing)
        catalogue_models = [
            {"id": "online"},
            {"id": "offline-b"},
            {"id": "offline-a"},
        ]

        result = catalog.merge_catalogue_status(existing, catalogue_models)

        self.assertEqual(existing, before)
        self.assertEqual(result[0], before[0])
        self.assertEqual(
            result[1:],
            [
                {
                    "id": "offline-b",
                    "state": "offline",
                    "online": False,
                    "loaded": False,
                    "source": "unknown",
                },
                {
                    "id": "offline-a",
                    "state": "offline",
                    "online": False,
                    "loaded": False,
                    "source": "unknown",
                },
            ],
        )

    def test_status_join_uses_exact_ids(self):
        result = catalog.merge_catalogue_status(
            [{"id": "Model-X", "state": "online"}],
            [{"id": "model-x"}],
        )

        self.assertEqual([item["id"] for item in result], ["Model-X", "model-x"])

    def test_disabled_models_are_omitted_not_reported_offline(self):
        existing = [
            {"id": "paid", "state": "online", "online": True, "loaded": False},
            {"id": "local", "state": "online", "online": True, "loaded": True},
        ]

        result = catalog.merge_catalogue_status(
            existing,
            [{"id": "paid"}, {"id": "offline-enabled"}],
            disabled_model_ids={"paid"},
        )

        self.assertEqual(
            result,
            [
                {
                    "id": "local",
                    "state": "online",
                    "online": True,
                    "loaded": True,
                },
                {
                    "id": "offline-enabled",
                    "state": "offline",
                    "online": False,
                    "loaded": False,
                    "source": "unknown",
                },
            ],
        )


class TestOpenAIModelFiltering(unittest.TestCase):
    def test_filter_removes_direct_and_base_chain_disabled_ids_without_mutation(self):
        response = {
            "object": "list",
            "data": [
                {"id": "paid-base", "urlIdx": 1},
                {"id": "paid-preset", "urlIdx": 1},
                {"id": "local", "urlIdx": 0},
                {"id": "provider-only", "urlIdx": 2},
                "paid-base",
                {"name": "paid-preset"},
            ],
        }
        records = [
            model("paid-base", active=False),
            model("paid-preset", base_model_id="paid-base"),
            model("local"),
        ]
        before = copy.deepcopy(response)

        result = catalog.filter_inactive_model_response(response, records)

        self.assertEqual(
            [item["id"] for item in result["data"]],
            ["local", "provider-only"],
        )
        self.assertEqual(response, before)
        self.assertEqual(result["object"], "list")

    def test_string_and_name_only_provider_entries_are_filtered(self):
        records = [model("paid", active=False), model("local", active=True)]

        result = catalog.filter_inactive_model_response(
            {
                "data": [
                    "paid",
                    "local",
                    {"name": "paid"},
                    {"name": "local"},
                ]
            },
            records,
        )

        self.assertEqual(result["data"], ["local", {"name": "local"}])

    def test_active_and_reenabled_ids_are_available(self):
        records = [
            model("local", active=True),
            model("preset", active=True, base_model_id="local"),
        ]

        self.assertEqual(catalog.unavailable_registered_model_ids(records), set())


if __name__ == "__main__":
    unittest.main()

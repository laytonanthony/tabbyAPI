import ast
import asyncio
import copy
import importlib.util
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import time
import unittest


OVERLAY_DIRECTORY = Path(__file__).parents[1]
INSTALLER_PATH = OVERLAY_DIRECTORY / "install.py"
SPEC = importlib.util.spec_from_file_location("zeta_overlay_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)

DEFAULT_BACKEND = Path(
    "/media/anthony/UltraGPT/Zeta WEBUI/Zeta WEBUI/backend"
)
RUNTIME_BACKEND = Path(os.environ.get("ZETA_OPENWEBUI_BACKEND", DEFAULT_BACKEND))
RUNTIME_MODELS = RUNTIME_BACKEND / "open_webui" / "utils" / "models.py"


class CustomModel(SimpleNamespace):
    def model_dump(self):
        return {
            "id": self.id,
            "name": self.name,
            "user_id": "owner",
            "base_model_id": self.base_model_id,
            "is_active": self.is_active,
            "meta": {},
            "params": {},
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "access_grants": [],
        }


class EmptyFunctions:
    @staticmethod
    def get_global_action_functions():
        return []

    @staticmethod
    def get_global_filter_functions():
        return []

    @staticmethod
    def get_functions_by_type(_function_type, active_only=True):
        return []

    @staticmethod
    def get_functions_by_ids(_ids):
        return []

    @staticmethod
    def get_function_valves_by_ids(_ids):
        return {}


def custom_model(model_id, *, active, base_model_id=None, name=None):
    return CustomModel(
        id=model_id,
        name=name or model_id,
        is_active=active,
        base_model_id=base_model_id,
        created_at=1,
        meta=SimpleNamespace(model_dump=lambda: {}),
    )


def load_transformed_get_all_models(base_models, custom_models):
    source = RUNTIME_MODELS.read_text(encoding="utf-8")
    transformed = installer.transform_models(
        source,
        installer.sha256(RUNTIME_MODELS),
    )
    tree = ast.parse(transformed)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_all_models"
    )

    async def get_all_base_models(_request, user=None):
        return [dict(model) for model in base_models]

    class Registry:
        @staticmethod
        def get_all_models():
            return list(custom_models)

    namespace = {
        "UserModel": object,
        "Models": Registry,
        "Functions": EmptyFunctions,
        "RedisDict": type("RedisDict", (), {}),
        "DEFAULT_ARENA_MODEL": {"id": "arena", "name": "Arena", "meta": {}},
        "copy": copy,
        "get_all_base_models": get_all_base_models,
        "get_function_module_from_cache": lambda *_args, **_kwargs: None,
        "log": logging.getLogger("zeta_model_merge_runtime_test"),
        "time": time,
    }
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(RUNTIME_MODELS), "exec"), namespace)
    return namespace["get_all_models"]


def request_state():
    config = SimpleNamespace(
        ENABLE_BASE_MODELS_CACHE=False,
        ENABLE_EVALUATION_ARENA_MODELS=False,
        EVALUATION_ARENA_MODELS=[],
        DEFAULT_MODEL_METADATA={},
    )
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                MODELS={},
                BASE_MODELS=[],
                FUNCTIONS={},
                config=config,
            )
        )
    )


@unittest.skipUnless(RUNTIME_MODELS.is_file(), "reviewed OpenWebUI runtime unavailable")
class TestRuntimeModelMerge(unittest.TestCase):
    def run_merge(self, base_models, custom_models):
        function = load_transformed_get_all_models(base_models, custom_models)
        return asyncio.run(function(request_state()))

    def test_qwen_survives_disabled_cloud_and_colonless_collision(self):
        result = self.run_merge(
            [
                {"id": "Qwen3.8", "name": "Qwen", "owned_by": "openai"},
                {
                    "id": "deepseek-v4-flash:cloud",
                    "name": "DeepSeek cloud",
                    "owned_by": "ollama",
                },
            ],
            [
                custom_model("deepseek-v4-flash:cloud", active=False),
                custom_model("deepseek-v4-flash", active=False),
                custom_model("Qwen3.8", active=True, name="Zeta Qwen"),
            ],
        )

        self.assertEqual([model["id"] for model in result], ["Qwen3.8"])
        self.assertEqual(result[0]["name"], "Zeta Qwen")

    def test_exact_disabled_removal_and_missing_exact_noop(self):
        exact = self.run_merge(
            [{"id": "paid:cloud", "name": "Paid", "owned_by": "ollama"}],
            [custom_model("paid:cloud", active=False)],
        )
        missing = self.run_merge(
            [{"id": "local:latest", "name": "Local", "owned_by": "ollama"}],
            [custom_model("local", active=False)],
        )

        self.assertEqual(exact, [])
        self.assertEqual([model["id"] for model in missing], ["local:latest"])

    def test_active_preset_keeps_ollama_shorthand_resolution(self):
        result = self.run_merge(
            [{"id": "local:latest", "name": "Local", "owned_by": "ollama"}],
            [custom_model("friendly", active=True, base_model_id="local")],
        )

        self.assertEqual([model["id"] for model in result], ["local:latest", "friendly"])
        self.assertTrue(result[1]["preset"])
        self.assertEqual(result[1]["owned_by"], "ollama")


if __name__ == "__main__":
    unittest.main()

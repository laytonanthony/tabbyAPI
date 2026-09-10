import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

try:
    import cryptography  # noqa: F401
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ModuleNotFoundError as exc:  # The deployed OpenWebUI runtime provides it.
    raise unittest.SkipTest(
        "cryptography is unavailable in this test environment"
    ) from exc


OVERLAY_ROOT = (
    Path(__file__).resolve().parents[1] / "deploy" / "zeta-openwebui" / "files"
)
CORE_PATH = OVERLAY_ROOT / "open_webui" / "utils" / "zeta_desktop_updates.py"
ROUTER_PATH = OVERLAY_ROOT / "open_webui" / "routers" / "zeta_desktop_updates.py"


def load_route_modules():
    package = ModuleType("open_webui")
    package.__path__ = [str(OVERLAY_ROOT / "open_webui")]
    utils_package = ModuleType("open_webui.utils")
    utils_package.__path__ = [str(OVERLAY_ROOT / "open_webui" / "utils")]
    routers_package = ModuleType("open_webui.routers")
    routers_package.__path__ = [str(OVERLAY_ROOT / "open_webui" / "routers")]
    names = {
        "open_webui": package,
        "open_webui.utils": utils_package,
        "open_webui.routers": routers_package,
    }
    previous = {name: sys.modules.get(name) for name in names}
    sys.modules.update(names)
    try:
        core_name = "open_webui.utils.zeta_desktop_updates"
        core_spec = importlib.util.spec_from_file_location(core_name, CORE_PATH)
        assert core_spec is not None and core_spec.loader is not None
        core = importlib.util.module_from_spec(core_spec)
        sys.modules[core_name] = core
        core_spec.loader.exec_module(core)

        router_name = "open_webui.routers.zeta_desktop_updates"
        router_spec = importlib.util.spec_from_file_location(router_name, ROUTER_PATH)
        assert router_spec is not None and router_spec.loader is not None
        route_module = importlib.util.module_from_spec(router_spec)
        sys.modules[router_name] = route_module
        router_spec.loader.exec_module(route_module)
        return core, route_module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        sys.modules.pop("open_webui.utils.zeta_desktop_updates", None)
        sys.modules.pop("open_webui.routers.zeta_desktop_updates", None)


updates, update_routes = load_route_modules()


def route_test_client() -> TestClient:
    app = FastAPI()
    app.include_router(update_routes.router, prefix="/api/desktop/updates")

    @app.get("/api/v1/models/catalog")
    def existing_catalogue():
        return {"schema_version": 1, "models": []}

    @app.get("/api/v1/models/status")
    def existing_status():
        return {"data": []}

    @app.get("/openai/models")
    def existing_openai_models():
        return {"data": []}

    @app.post("/openai/responses")
    def existing_responses():
        return {"id": "resp_compatibility_probe", "status": "completed"}

    @app.get("/{unmatched_path:path}")
    def spa_fallback(unmatched_path: str):
        return HTMLResponse("<html>SPA fallback</html>")

    return TestClient(app)


class FakeStore:
    def __init__(self, release=None, error=None):
        self.selected_release = release
        self.error = error
        self.requested_versions = []

    def release(self, version):
        self.requested_versions.append(version)
        if self.error is not None:
            raise self.error
        if self.selected_release is None or self.selected_release.version != version:
            raise updates.UpdateNotFoundError("not retained")
        return self.selected_release


class DesktopUpdateRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.client = route_test_client()

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def release(self, version="1.2.3", installer_bytes=b"portable installer"):
        filename = f"Zeta-Setup-{version}.exe"
        installer_path = self.base / filename
        installer_path.write_bytes(installer_bytes)
        import hashlib

        installer_sha256 = hashlib.sha256(installer_bytes).hexdigest()
        manifest = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "product": "Zeta",
                    "channel": "stable",
                    "version": version,
                    "publishedAt": "2026-09-10T12:00:00.0000000+00:00",
                    "minimumSupportedVersion": "1.0.0",
                    "mandatory": False,
                    "installer": {
                        "url": (
                            "https://updates.example.test/api/desktop/updates/"
                            f"stable/{filename}"
                        ),
                        "size": len(installer_bytes),
                        "sha256": installer_sha256,
                    },
                },
                indent=2,
            )
            + "\n"
        ).encode()
        signature = b"Y2Fub25pY2FsLWJhc2U2NA==\r\n"
        return SimpleNamespace(
            version=version,
            manifest=manifest,
            signature=signature,
            installer=SimpleNamespace(filename=filename, size=len(installer_bytes)),
            installer_path=installer_path,
            installer_sha256=installer_sha256,
        )

    def test_no_release_exact_and_unknown_paths_are_json_not_spa_html(self):
        missing_root = self.base / "not-published"
        with (
            mock.patch.object(update_routes, "get_release_root", return_value=missing_root),
            mock.patch.object(
                update_routes,
                "get_public_key_path",
                return_value=self.base / "public.pem",
            ),
        ):
            responses = [
                self.client.get("/api/desktop/updates/stable/latest.json"),
                self.client.get("/api/desktop/updates/stable/latest.json.sig"),
                self.client.get(
                    "/api/desktop/updates/stable/Zeta-Setup-1.2.3.exe"
                ),
                self.client.get("/api/desktop/updates/stable/unknown.json"),
                self.client.get("/api/desktop/updates/not-a-route"),
            ]

        for response in responses:
            with self.subTest(url=response.request.url):
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["content-type"], "application/json")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("SPA fallback", response.text)
        self.assertEqual(
            responses[0].json(),
            {"detail": "No stable Zeta desktop release is published"},
        )
        self.assertEqual(responses[1].json(), responses[0].json())

    def test_existing_model_and_responses_routes_are_not_shadowed(self):
        checks = (
            self.client.get("/api/v1/models/catalog"),
            self.client.get("/api/v1/models/status"),
            self.client.get("/openai/models"),
            self.client.post("/openai/responses", json={"input": "probe"}),
        )
        self.assertEqual([response.status_code for response in checks], [200] * 4)
        self.assertEqual(checks[0].json(), {"schema_version": 1, "models": []})
        self.assertEqual(checks[1].json(), {"data": []})
        self.assertEqual(checks[2].json(), {"data": []})
        self.assertEqual(checks[3].json()["status"], "completed")

    def test_manifest_and_signature_preserve_exact_bytes_etag_and_no_cache(self):
        release = self.release()
        with mock.patch.object(
            update_routes, "_current_release", return_value=(None, release)
        ):
            manifest = self.client.get(
                "/api/desktop/updates/stable/latest.json"
            )
            signature = self.client.get(
                "/api/desktop/updates/stable/latest.json.sig"
            )
            unchanged = self.client.get(
                "/api/desktop/updates/stable/latest.json",
                headers={"If-None-Match": manifest.headers["etag"]},
            )
            signature_unchanged = self.client.get(
                "/api/desktop/updates/stable/latest.json.sig",
                headers={"If-None-Match": signature.headers["etag"]},
            )

        self.assertEqual(manifest.content, release.manifest)
        self.assertEqual(
            manifest.headers["content-type"], "application/json; charset=utf-8"
        )
        self.assertEqual(manifest.headers["cache-control"], "no-cache")
        self.assertEqual(manifest.headers["x-content-type-options"], "nosniff")
        self.assertEqual(signature.content, release.signature)
        self.assertEqual(
            signature.headers["content-type"], "text/plain; charset=us-ascii"
        )
        self.assertEqual(signature.headers["cache-control"], "no-cache")
        self.assertNotEqual(signature.headers["etag"], manifest.headers["etag"])
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.content, b"")
        self.assertEqual(unchanged.headers["etag"], manifest.headers["etag"])
        self.assertEqual(unchanged.headers["cache-control"], "no-cache")
        self.assertEqual(signature_unchanged.status_code, 304)
        self.assertEqual(signature_unchanged.content, b"")
        self.assertEqual(
            signature_unchanged.headers["etag"], signature.headers["etag"]
        )

    def test_retained_installer_is_immutable_range_capable_and_conditional(self):
        body = b"0123456789abcdef"
        release = self.release("2.4.6", body)
        store = FakeStore(release)
        with mock.patch.object(update_routes, "_configured_store", return_value=store):
            response = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-2.4.6.exe"
            )
            ranged = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-2.4.6.exe",
                headers={"Range": "bytes=3-6"},
            )
            unchanged = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-2.4.6.exe",
                headers={"If-None-Match": response.headers["etag"]},
            )

        self.assertEqual(store.requested_versions, ["2.4.6"] * 3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, body)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["content-length"], str(len(body)))
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable, no-transform",
        )
        self.assertIn("Zeta-Setup-2.4.6.exe", response.headers["content-disposition"])
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, body[3:7])
        self.assertEqual(ranged.headers["content-range"], "bytes 3-6/16")
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.content, b"")

    def test_real_store_publication_is_served_and_same_size_tamper_fails_closed(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        version = "3.4.5"
        filename = f"Zeta-Setup-{version}.exe"
        installer_bytes = b"exact signed portable installer"
        installer = self.base / "build" / filename
        installer.parent.mkdir()
        installer.write_bytes(installer_bytes)
        manifest = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "product": "Zeta",
                    "channel": "stable",
                    "version": version,
                    "publishedAt": "2026-09-10T12:00:00.0000000+00:00",
                    "minimumSupportedVersion": "1.0.0",
                    "mandatory": False,
                    "releaseNotes": "Exact route-test bytes.",
                    "installer": {
                        "url": (
                            "https://updates.example.test/api/desktop/updates/"
                            f"stable/{filename}"
                        ),
                        "size": len(installer_bytes),
                        "sha256": hashlib.sha256(installer_bytes).hexdigest(),
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        signature = base64.b64encode(
            private_key.sign(manifest, padding.PKCS1v15(), hashes.SHA256())
        ) + b"\n"
        summary = (
            json.dumps(
                {"product": "Zeta", "channel": "stable", "version": version},
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        store = updates.ReleaseStore(self.base / "store", public_key)
        published = store.publish(
            manifest_bytes=manifest,
            signature_bytes=signature,
            summary_bytes=summary,
            installer_source=installer,
        )

        with mock.patch.object(update_routes, "_configured_store", return_value=store):
            manifest_response = self.client.get(
                "/api/desktop/updates/stable/latest.json"
            )
            signature_response = self.client.get(
                "/api/desktop/updates/stable/latest.json.sig"
            )
            installer_response = self.client.get(
                f"/api/desktop/updates/stable/{filename}"
            )
            published.release.installer_path.write_bytes(b"X" * len(installer_bytes))
            tampered_response = self.client.get(
                f"/api/desktop/updates/stable/{filename}"
            )

        self.assertEqual(manifest_response.content, manifest)
        self.assertEqual(signature_response.content, signature)
        self.assertEqual(installer_response.content, installer_bytes)
        self.assertEqual(installer_response.status_code, 200)
        self.assertEqual(tampered_response.status_code, 503)
        self.assertEqual(
            tampered_response.json(), {"detail": "Desktop update feed unavailable"}
        )

    def test_invalid_or_multiple_installer_ranges_return_json_416(self):
        release = self.release(installer_bytes=b"0123456789")
        store = FakeStore(release)
        with mock.patch.object(update_routes, "_configured_store", return_value=store):
            responses = [
                self.client.get(
                    "/api/desktop/updates/stable/Zeta-Setup-1.2.3.exe",
                    headers={"Range": value},
                )
                for value in ("bytes=99-100", "bytes=1-2,5-6", "items=1-2")
            ]
        for response in responses:
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.headers["content-range"], "bytes */10")
            self.assertEqual(response.headers["accept-ranges"], "bytes")
            self.assertEqual(response.headers["content-type"], "application/json")

    def test_invalid_mismatched_and_traversal_installer_paths_are_json_404(self):
        release = self.release()
        store = FakeStore(release)
        with mock.patch.object(update_routes, "_configured_store", return_value=store):
            invalid = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-v1.2.3.exe"
            )
            mismatched = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-1.2.4.exe"
            )
            traversal = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-../private.exe"
            )

        for response in (invalid, mismatched, traversal):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["content-type"], "application/json")
            self.assertNotIn("SPA fallback", response.text)

    def test_store_failures_are_generic_and_do_not_leak_paths_or_secrets(self):
        release = self.release()
        secret = "sk-private /internal/provider"
        with mock.patch.object(
            update_routes,
            "_current_release",
            side_effect=updates.StoreError(secret),
        ):
            response = self.client.get(
                "/api/desktop/updates/stable/latest.json"
            )
        store = FakeStore(release, error=updates.StoreError(secret))
        with mock.patch.object(update_routes, "_configured_store", return_value=store):
            installer = self.client.get(
                "/api/desktop/updates/stable/Zeta-Setup-1.2.3.exe"
            )

        for failed in (response, installer):
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(
                failed.json(), {"detail": "Desktop update feed unavailable"}
            )
            self.assertNotIn(secret, failed.text)

    def test_configured_paths_inside_application_tree_are_rejected(self):
        application_root = ROUTER_PATH.resolve().parents[3]
        for path in (
            application_root / "update-state",
            application_root / "config" / "public.pem",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(
                updates.StoreError, "outside the application source tree"
            ):
                update_routes._external_config_path(path, label="test path")

    def test_update_router_has_only_public_get_and_head_routes(self):
        methods = set()
        for route in update_routes.router.routes:
            methods.update(route.methods or ())
        self.assertEqual(methods, {"GET"})
        response = self.client.post(
            "/api/desktop/updates/stable/latest.json", content=b"upload"
        )
        self.assertEqual(response.status_code, 405)


if __name__ == "__main__":
    unittest.main()

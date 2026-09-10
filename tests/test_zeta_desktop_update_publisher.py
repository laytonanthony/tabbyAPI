import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "zeta-openwebui"
    / "publish_desktop_release.py"
)
SPEC = importlib.util.spec_from_file_location("zeta_desktop_release_publisher", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publisher
SPEC.loader.exec_module(publisher)


class FakeStore:
    instances = []

    def __init__(self, root, public_key_path):
        self.root = root
        self.public_key_path = public_key_path
        self.publish_call = None
        self.rollback_call = None
        self.__class__.instances.append(self)

    def publish_files(self, **kwargs):
        self.publish_call = kwargs
        release = SimpleNamespace(version="1.2.3", installer_sha256="a" * 64)
        return SimpleNamespace(changed=True, release=release)

    def rollback(self, version):
        self.rollback_call = version
        return SimpleNamespace(version=version, installer_sha256="b" * 64)


class FakeUpdates:
    ReleaseStore = FakeStore

    @staticmethod
    def get_release_root():
        return Path("/external/default/releases")

    @staticmethod
    def get_public_key_path():
        return Path("/external/default/public.pem")


class DesktopUpdatePublisherTests(unittest.TestCase):
    def setUp(self):
        FakeStore.instances.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.release_root = self.base / "published"
        self.public_key = self.base / "public.pem"
        self.public_key.write_text("test public key")
        self.inputs = {}
        for name in (
            "Zeta-Setup-1.2.3.exe",
            "latest.json",
            "latest.json.sig",
            "release-summary.json",
        ):
            path = self.base / name
            path.write_bytes(b"input")
            self.inputs[name] = path

    def tearDown(self):
        self.temporary.cleanup()

    def test_publish_passes_exact_absolute_inputs_to_store(self):
        args = publisher.build_parser().parse_args(
            [
                "--release-dir",
                str(self.release_root),
                "--public-key",
                str(self.public_key),
                "publish",
                "--exe",
                str(self.inputs["Zeta-Setup-1.2.3.exe"]),
                "--latest",
                str(self.inputs["latest.json"]),
                "--sig",
                str(self.inputs["latest.json.sig"]),
                "--release-summary",
                str(self.inputs["release-summary.json"]),
            ]
        )

        self.assertEqual(publisher.run(args, FakeUpdates), 0)
        store = FakeStore.instances[-1]
        self.assertEqual(store.root, self.release_root)
        self.assertEqual(store.public_key_path, self.public_key)
        self.assertEqual(
            store.publish_call,
            {
                "manifest_path": self.inputs["latest.json"],
                "signature_path": self.inputs["latest.json.sig"],
                "summary_path": self.inputs["release-summary.json"],
                "installer_source": self.inputs["Zeta-Setup-1.2.3.exe"],
            },
        )

    def test_rollback_uses_configured_defaults_and_exact_version(self):
        args = publisher.build_parser().parse_args(["rollback", "1.2.2"])
        self.assertEqual(publisher.run(args, FakeUpdates), 0)
        store = FakeStore.instances[-1]
        self.assertEqual(store.root, Path("/external/default/releases"))
        self.assertEqual(
            store.public_key_path, Path("/external/default/public.pem")
        )
        self.assertEqual(store.rollback_call, "1.2.2")

    def test_artifact_inputs_must_be_absolute_regular_non_symlink_files(self):
        with self.assertRaises(SystemExit):
            publisher.build_parser().parse_args(
                [
                    "publish",
                    "--exe",
                    "relative.exe",
                    "--latest",
                    str(self.inputs["latest.json"]),
                    "--sig",
                    str(self.inputs["latest.json.sig"]),
                    "--release-summary",
                    str(self.inputs["release-summary.json"]),
                ]
            )

        options = {
            "--exe": "Zeta-Setup-1.2.3.exe",
            "--latest": "latest.json",
            "--sig": "latest.json.sig",
            "--release-summary": "release-summary.json",
        }
        for option, name in options.items():
            with self.subTest(option=option):
                link = self.base / f"linked-{name}"
                link.symlink_to(self.inputs[name])
                arguments = ["publish"]
                for candidate_option, candidate_name in options.items():
                    candidate_path = (
                        link
                        if candidate_option == option
                        else self.inputs[candidate_name]
                    )
                    arguments.extend((candidate_option, str(candidate_path)))
                with self.assertRaises(SystemExit):
                    publisher.build_parser().parse_args(arguments)

    def test_recognizable_openwebui_source_paths_are_rejected(self):
        source = self.base / "openwebui"
        marker = source / "backend" / "open_webui" / "main.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# source marker\n")
        with self.assertRaisesRegex(
            publisher.PublisherError, "outside the application source tree"
        ):
            publisher.external_openwebui_path(
                source / "state" / "desktop-updates",
                label="update-store root",
            )


if __name__ == "__main__":
    unittest.main()

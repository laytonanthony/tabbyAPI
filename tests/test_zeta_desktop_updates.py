import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

try:
    import cryptography  # noqa: F401
except ModuleNotFoundError as exc:  # The deployed OpenWebUI runtime provides it.
    raise unittest.SkipTest("cryptography is unavailable in this test environment") from exc


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "zeta-openwebui"
    / "files"
    / "open_webui"
    / "utils"
    / "zeta_desktop_updates.py"
)
SPEC = importlib.util.spec_from_file_location("zeta_desktop_updates", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
updates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updates
SPEC.loader.exec_module(updates)


# Disposable test-only RSA key material.  Production code receives only PEM
# public-key bytes and intentionally has no signing function.
TEST_RSA_N = int(
    "22929526207791036594490277089515885111264006620475981129326221925166916801118979"
    "90368918763593782690723663837382092456649329495813277548291033735935137991347905"
    "72850447075009355566990499354085233653683206893958287779992433835006425874700703"
    "33010813937238908930278058762873848251337025383805042814384105491237879829340300"
    "32271093008460145545896865428217702511401712408688794896551390639858264145642443"
    "29674538427305592174857207847508206833832800682183942111344414557406287027481371"
    "49087838125423631059027252889063126867777445860179567090469952868382500848629445"
    "005081219051777341519453629008341260264500698710648156479"
)
TEST_RSA_D = int(
    "27464910009789834332781280063582357770941979640620787015763742941019622028592091"
    "83269910477167278655138434948723534153943152195268966958219725472189882544529206"
    "39772343796454436608461696435535511875949947986263112298845019699528578225491023"
    "56552007172639186276863872513017029887391191123009839646751488183196510109194383"
    "31991425518702514416490116421453444452247785927982933643978417383757954595375165"
    "55030350181224063095659700061524482788144898205775683668865310959781713608577664"
    "92435402209521025164529455218131045771713166178620763016709033477892628810798253"
    "07685946966829256309530199063523124293045292302873433473"
)
TEST_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtaMHo2aETTnHovemSNOk
+air3KFmnnx/33yryXYU3N60Sm2bLk6WrYF2Y+PmF3YwXiAnpTBc/vEQv8Cym3oL
n2E0AJDXbvb87+oIFxDsEUM75XOd2SAHptc+Hcn/nQGBBp7LW719g9R9Go95vEdS
J0+uPPnraNtqUEN7mFfXMDLg/NCeMvUxcQDMZkt5eHs72ScnP4syZZe66+tN3nXi
jDxoNwqwtg5i2VJyAh4zkmi9zN4foRvmETCZAYdndS41yv7iYROeRhZPbkvLkG15
Fdh5VNjdjY4JxSHI5SjPydbZkgCb2O1Vci97vEhYymC6CS09yGvd5Hg8uvatwk3x
PwIDAQAB
-----END PUBLIC KEY-----
"""
DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def sign_for_test(data: bytes) -> bytes:
    width = (TEST_RSA_N.bit_length() + 7) // 8
    digest_info = DIGEST_INFO_PREFIX + hashlib.sha256(data).digest()
    encoded = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
    raw = pow(int.from_bytes(encoded, "big"), TEST_RSA_D, TEST_RSA_N).to_bytes(
        width, "big"
    )
    import base64

    return base64.b64encode(raw) + b"\n"


class DesktopUpdateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "store"
        self.builds = self.base / "builds"
        self.builds.mkdir()
        self.store = updates.ReleaseStore(
            self.root,
            TEST_PUBLIC_KEY,
            product="Zeta",
            channel="stable",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def release_inputs(
        self,
        version="1.2.3",
        *,
        installer_bytes=b"signed installer bytes",
        manifest_changes=None,
        summary_changes=None,
    ):
        filename = f"Zeta-Setup-{version}.exe"
        installer_directory = self.builds / version.replace("+", "_")
        installer_directory.mkdir(exist_ok=True)
        installer = installer_directory / filename
        installer.write_bytes(installer_bytes)
        document = {
            "schema_version": 1,
            "product": "Zeta",
            "channel": "stable",
            "version": version,
            "installer": {
                "filename": filename,
                "url": f"https://updates.example.test/releases/{version}/{filename}",
                "size": len(installer_bytes),
                "sha256": hashlib.sha256(installer_bytes).hexdigest(),
            },
        }
        if manifest_changes:
            for key, value in manifest_changes.items():
                if key.startswith("installer."):
                    document["installer"][key.split(".", 1)[1]] = value
                else:
                    document[key] = value
        manifest = (json.dumps(document, indent=2) + "\n").encode()
        summary_document = {
            "product": "Zeta",
            "channel": "stable",
            "version": version,
            "notes": ["Exact publisher-supplied summary"],
        }
        if summary_changes:
            summary_document.update(summary_changes)
        summary = (json.dumps(summary_document, separators=(",", ":")) + "\n").encode()
        return manifest, sign_for_test(manifest), summary, installer

    def publish(self, version="1.2.3", **kwargs):
        manifest, signature, summary, installer = self.release_inputs(version, **kwargs)
        result = self.store.publish(
            manifest_bytes=manifest,
            signature_bytes=signature,
            summary_bytes=summary,
            installer_source=installer,
        )
        return result, (manifest, signature, summary, installer)

    def test_publish_preserves_exact_artifacts_and_pointer_is_router_friendly(self):
        result, supplied = self.publish()
        manifest, signature, summary, installer = supplied

        self.assertTrue(result.changed)
        self.assertEqual((self.root / "current-version").read_bytes(), b"1.2.3\n")
        release = self.store.current_release()
        self.assertEqual(release.version, "1.2.3")
        self.assertEqual(release.manifest_path.read_bytes(), manifest)
        self.assertEqual(release.signature_path.read_bytes(), signature)
        self.assertEqual(release.summary_path.read_bytes(), summary)
        self.assertEqual(release.installer_path.read_bytes(), installer.read_bytes())
        self.assertEqual(
            release.installer_sha256, hashlib.sha256(installer.read_bytes()).hexdigest()
        )
        for path in (
            release.manifest_path,
            release.signature_path,
            release.summary_path,
            release.installer_path,
            self.root / "current-version",
            self.root / updates.STORE_MARKER_FILENAME,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            (self.root / updates.STORE_MARKER_FILENAME).read_bytes(),
            updates.STORE_MARKER_BYTES,
        )
        self.assertEqual(stat.S_IMODE(release.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.root / "releases").stat().st_mode), 0o700
        )

    def test_signature_is_over_exact_manifest_bytes(self):
        manifest, signature, summary, installer = self.release_inputs()
        updates.verify_rsa_sha256(TEST_PUBLIC_KEY, manifest, signature)
        with self.assertRaises(updates.UpdateSignatureError):
            updates.verify_rsa_sha256(TEST_PUBLIC_KEY, manifest + b" ", signature)
        with self.assertRaises(updates.UpdateSignatureError):
            self.store.publish(
                manifest_bytes=manifest + b" ",
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )

    def test_signature_file_is_strict_canonical_base64_but_keeps_line_ending(self):
        manifest, signature, summary, installer = self.release_inputs()
        for ending in (b"", b"\n", b"\r\n"):
            exact_signature = signature.rstrip(b"\r\n") + ending
            updates.verify_rsa_sha256(TEST_PUBLIC_KEY, manifest, exact_signature)
        invalid = signature[:20] + b" \n" + signature[20:]
        with self.assertRaises(updates.UpdateSignatureError):
            updates.verify_rsa_sha256(TEST_PUBLIC_KEY, manifest, invalid)

        crlf_signature = signature.rstrip(b"\n") + b"\r\n"
        result = self.store.publish(
            manifest_bytes=manifest,
            signature_bytes=crlf_signature,
            summary_bytes=summary,
            installer_source=installer,
        )
        self.assertEqual(result.release.signature_path.read_bytes(), crlf_signature)

    def test_same_release_is_idempotent_but_equal_version_mutation_is_refused(self):
        first, supplied = self.publish()
        manifest, signature, summary, installer = supplied
        second = self.store.publish(
            manifest_bytes=manifest,
            signature_bytes=signature,
            summary_bytes=summary,
            installer_source=installer,
        )
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)

        changed_manifest, changed_signature, changed_summary, changed_installer = (
            self.release_inputs("1.2.3", installer_bytes=b"different bytes")
        )
        with self.assertRaises(updates.UpdateConflictError):
            self.store.publish(
                manifest_bytes=changed_manifest,
                signature_bytes=changed_signature,
                summary_bytes=changed_summary,
                installer_source=changed_installer,
            )

    def test_normal_publication_refuses_downgrade_and_equal_precedence_build(self):
        self.publish("2.0.0")
        for version in ("1.99.99", "2.0.0+replacement"):
            manifest, signature, summary, installer = self.release_inputs(version)
            with self.assertRaises(updates.UpdateVersionError):
                self.store.publish(
                    manifest_bytes=manifest,
                    signature_bytes=signature,
                    summary_bytes=summary,
                    installer_source=installer,
                )
        self.assertEqual((self.root / "current-version").read_text(), "2.0.0\n")

    def test_semver_prerelease_order_is_numeric_not_lexical(self):
        versions = [
            updates.SemVer.parse(value)
            for value in ("1.0.0-alpha.2", "1.0.0-alpha.10", "1.0.0")
        ]
        self.assertLess(versions[0], versions[1])
        self.assertLess(versions[1], versions[2])
        build_a = updates.SemVer.parse("1.0.0+build-a")
        build_b = updates.SemVer.parse("1.0.0+build-b")
        self.assertEqual(build_a, build_b)
        self.assertEqual(hash(build_a), hash(build_b))
        for invalid in ("1.0", "01.0.0", "1.0.0-01", "v1.0.0", "1.0.0/"):
            with self.assertRaises(updates.UpdateValidationError):
                updates.SemVer.parse(invalid)

    def test_rollback_only_selects_an_older_revalidated_release(self):
        self.publish("1.0.0")
        self.publish("2.0.0")
        rolled_back = self.store.rollback("1.0.0")
        self.assertEqual(rolled_back.version, "1.0.0")
        self.assertEqual((self.root / "current-version").read_text(), "1.0.0\n")
        with self.assertRaises(updates.UpdateVersionError):
            self.store.rollback("2.0.0")
        with self.assertRaises(updates.UpdateNotFoundError):
            self.store.rollback("0.9.0")

    def test_retained_release_lookup_survives_pointer_change(self):
        first, _ = self.publish("1.0.0")
        self.publish("2.0.0")
        retained = self.store.release("1.0.0")
        retained_alias = self.store.get_release("1.0.0")
        self.assertEqual(retained.installer_path, first.release.installer_path)
        self.assertEqual(retained_alias.manifest, first.release.manifest)
        self.assertEqual(self.store.current_release().version, "2.0.0")

    def test_rollback_refuses_a_tampered_archived_release(self):
        first, _ = self.publish("1.0.0")
        self.publish("2.0.0")
        first.release.installer_path.write_bytes(b"tampered")
        with self.assertRaises(updates.UpdateValidationError):
            self.store.rollback("1.0.0")
        self.assertEqual((self.root / "current-version").read_text(), "2.0.0\n")

    def test_explicit_rollback_can_recover_a_tampered_current_release(self):
        self.publish("1.0.0")
        current, _ = self.publish("2.0.0")
        current.release.manifest_path.write_bytes(b"corrupt current manifest")

        restored = self.store.rollback("1.0.0")

        self.assertEqual(restored.version, "1.0.0")
        self.assertEqual(self.store.current_release().version, "1.0.0")

    def test_summary_is_exact_bounded_json_and_matching_fields_are_enforced(self):
        manifest, signature, _, installer = self.release_inputs()
        mismatched = b'{"version":"9.9.9"}\n'
        with self.assertRaises(updates.UpdateValidationError):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=mismatched,
                installer_source=installer,
            )
        with self.assertRaises(updates.UpdateValidationError):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=b"[]\n",
                installer_source=installer,
            )

    def test_manifest_rejects_unsafe_or_non_https_installer_urls(self):
        unsafe_urls = (
            "http://updates.example.test/releases/Zeta-Setup-1.2.3.exe",
            "https://user:secret@updates.example.test/releases/Zeta-Setup-1.2.3.exe",
            "https://updates.example.test/releases/../Zeta-Setup-1.2.3.exe",
            "https://updates.example.test/releases/%2e%2e/Zeta-Setup-1.2.3.exe",
            "https://updates.example.test/releases/Zeta-Setup-1.2.3.exe?token=secret",
            "https://updates.example.test/releases/other.exe",
        )
        for url in unsafe_urls:
            with self.subTest(url=url):
                manifest, _, summary, installer = self.release_inputs(
                    manifest_changes={"installer.url": url}
                )
                with self.assertRaises(updates.UpdateValidationError):
                    self.store.publish(
                        manifest_bytes=manifest,
                        signature_bytes=sign_for_test(manifest),
                        summary_bytes=summary,
                        installer_source=installer,
                    )

    def test_manifest_requires_exact_versioned_installer_filename(self):
        manifest, _, summary, installer = self.release_inputs(
            manifest_changes={
                "installer.filename": "Zeta.exe",
                "installer.url": "https://updates.example.test/releases/Zeta.exe",
            }
        )
        with self.assertRaisesRegex(updates.UpdateValidationError, "Zeta-Setup"):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=sign_for_test(manifest),
                summary_bytes=summary,
                installer_source=installer,
            )

    def test_installer_hash_size_and_symlink_are_checked(self):
        manifest, signature, summary, installer = self.release_inputs()
        installer.write_bytes(b"x" * installer.stat().st_size)
        with self.assertRaises(updates.UpdateValidationError):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )

        manifest, signature, summary, installer = self.release_inputs("1.2.4")
        real = installer.with_suffix(".real")
        installer.rename(real)
        installer.symlink_to(real)
        with self.assertRaises(updates.UpdateValidationError):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )

    def test_source_size_mismatch_or_oversize_preserves_current_and_cleans_stage(self):
        self.publish("1.0.0")
        for version, source_size in (
            ("2.0.0", len(b"signed installer bytes") + 1),
            ("3.0.0", updates.MAX_INSTALLER_BYTES + 1),
        ):
            with self.subTest(version=version, source_size=source_size):
                manifest, signature, summary, installer = self.release_inputs(version)
                with installer.open("r+b") as handle:
                    handle.truncate(source_size)
                with self.assertRaises(updates.UpdateValidationError):
                    self.store.publish(
                        manifest_bytes=manifest,
                        signature_bytes=signature,
                        summary_bytes=summary,
                        installer_source=installer,
                    )
                self.assertEqual(
                    (self.root / "current-version").read_text(), "1.0.0\n"
                )
                self.assertFalse((self.root / "releases" / version).exists())
                self.assertFalse(
                    any(
                        child.name.startswith(".stage-")
                        for child in (self.root / "releases").iterdir()
                    )
                )

    def test_metadata_staging_failure_preserves_current_and_cleans_stage(self):
        self.publish("1.0.0")
        manifest, signature, summary, installer = self.release_inputs("2.0.0")
        with mock.patch.object(
            updates,
            "_write_file",
            side_effect=OSError("simulated metadata write failure"),
        ):
            with self.assertRaises(OSError):
                self.store.publish(
                    manifest_bytes=manifest,
                    signature_bytes=signature,
                    summary_bytes=summary,
                    installer_source=installer,
                )
        self.assertEqual((self.root / "current-version").read_text(), "1.0.0\n")
        self.assertFalse((self.root / "releases" / "2.0.0").exists())
        self.assertFalse(
            any(
                child.name.startswith(".stage-")
                for child in (self.root / "releases").iterdir()
            )
        )

    def test_invalid_current_pointer_fails_closed(self):
        self.publish()
        (self.root / "current-version").write_text("../1.2.3\n")
        with self.assertRaises(updates.UpdateValidationError):
            self.store.current_release()
        manifest, signature, summary, installer = self.release_inputs("2.0.0")
        with self.assertRaises(updates.UpdateValidationError):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )

    def test_release_is_committed_before_pointer_and_retry_recovers_orphan(self):
        manifest, signature, summary, installer = self.release_inputs()
        with mock.patch.object(
            self.store,
            "_replace_current_unlocked",
            side_effect=OSError("simulated pointer failure"),
        ):
            with self.assertRaises(OSError):
                self.store.publish(
                    manifest_bytes=manifest,
                    signature_bytes=signature,
                    summary_bytes=summary,
                    installer_source=installer,
                )
        self.assertFalse((self.root / "current-version").exists())
        self.assertTrue((self.root / "releases" / "1.2.3" / "latest.json").is_file())

        recovered = self.store.publish(
            manifest_bytes=manifest,
            signature_bytes=signature,
            summary_bytes=summary,
            installer_source=installer,
        )
        self.assertTrue(recovered.changed)
        self.assertEqual((self.root / "current-version").read_text(), "1.2.3\n")

    def test_publish_files_requires_contract_names(self):
        manifest, signature, summary, installer = self.release_inputs()
        input_directory = self.base / "input"
        input_directory.mkdir()
        paths = {
            "manifest": input_directory / "latest.json",
            "signature": input_directory / "latest.json.sig",
            "summary": input_directory / "release-summary.json",
        }
        paths["manifest"].write_bytes(manifest)
        paths["signature"].write_bytes(signature)
        paths["summary"].write_bytes(summary)
        result = self.store.publish_files(
            manifest_path=paths["manifest"],
            signature_path=paths["signature"],
            summary_path=paths["summary"],
            installer_source=installer,
        )
        self.assertTrue(result.changed)

        paths["summary"].rename(input_directory / "summary.json")
        with self.assertRaises(updates.UpdateValidationError):
            self.store.publish_files(
                manifest_path=paths["manifest"],
                signature_path=paths["signature"],
                summary_path=input_directory / "summary.json",
                installer_source=installer,
            )

    def test_etag_and_if_none_match_use_exact_bytes_and_weak_get_comparison(self):
        etag = updates.etag_bytes(b"exact\n")
        self.assertRegex(etag, r'^"[0-9a-f]{64}"$')
        self.assertTrue(updates.if_none_match(etag, etag))
        self.assertTrue(updates.if_none_match(f'"other", W/{etag}', etag))
        self.assertTrue(updates.if_none_match("*", etag))
        self.assertFalse(updates.if_none_match(None, etag))
        self.assertFalse(updates.if_none_match('"different"', etag))

    def test_config_paths_and_empty_store_interface(self):
        with mock.patch.dict(
            os.environ,
            {
                updates.RELEASE_ROOT_ENV: str(self.root),
                updates.PUBLIC_KEY_ENV: str(self.base / "public.pem"),
            },
        ):
            self.assertEqual(updates.get_release_root(), self.root)
            self.assertEqual(updates.get_public_key_path(), self.base / "public.pem")
        with self.assertRaises(updates.NoRelease):
            self.store.current_release()
        missing_key_store = updates.ReleaseStore(
            self.base / "empty-store",
            public_key_path=self.base / "missing-public.pem",
        )
        with self.assertRaises(updates.NoRelease):
            missing_key_store.current_release()
        with mock.patch.dict(os.environ, {updates.RELEASE_ROOT_ENV: "relative"}):
            with self.assertRaises(updates.UpdateValidationError):
                updates.get_release_root()

    def test_public_key_path_requires_private_owned_directory_and_file(self):
        manifest, signature, summary, installer = self.release_inputs()
        key_directory = self.base / "key-config"
        key_directory.mkdir(mode=0o700)
        key_directory.chmod(0o700)
        key_path = key_directory / "manifest-public-key.pem"
        key_path.write_bytes(TEST_PUBLIC_KEY)
        key_path.chmod(0o600)

        path_store = updates.ReleaseStore(
            self.base / "path-key-store",
            public_key_path=key_path,
        )
        result = path_store.publish(
            manifest_bytes=manifest,
            signature_bytes=signature,
            summary_bytes=summary,
            installer_source=installer,
        )
        self.assertTrue(result.changed)

        for key_mode, directory_mode, expected in (
            (0o644, 0o700, "0600"),
            (0o600, 0o755, "0700"),
        ):
            with self.subTest(key_mode=key_mode, directory_mode=directory_mode):
                key_path.chmod(key_mode)
                key_directory.chmod(directory_mode)
                unsafe_store = updates.ReleaseStore(
                    self.base / f"unsafe-key-{key_mode:o}-{directory_mode:o}",
                    public_key_path=key_path,
                )
                with self.assertRaisesRegex(updates.UpdateValidationError, expected):
                    unsafe_store.publish(
                        manifest_bytes=manifest,
                        signature_bytes=signature,
                        summary_bytes=summary,
                        installer_source=installer,
                    )
                self.assertFalse(unsafe_store.root.exists())

        key_directory.chmod(0o700)
        key_path.chmod(0o600)
        key_link = key_directory / "linked-public.pem"
        key_link.symlink_to(key_path)
        linked_store = updates.ReleaseStore(
            self.base / "linked-key-store",
            public_key_path=key_link,
        )
        with self.assertRaises(updates.UpdateSignatureError):
            linked_store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )

    def test_duplicate_json_keys_and_wrong_product_or_channel_are_rejected(self):
        duplicate = b'{"product":"Zeta","product":"Other"}\n'
        with self.assertRaises(updates.UpdateValidationError):
            updates.validate_manifest(
                duplicate,
                sign_for_test(duplicate),
                TEST_PUBLIC_KEY,
                expected_product="Zeta",
                expected_channel="stable",
            )
        for changes in ({"product": "Other"}, {"channel": "beta"}):
            manifest, _, summary, installer = self.release_inputs(
                manifest_changes=changes
            )
            with self.assertRaises(updates.UpdateValidationError):
                self.store.publish(
                    manifest_bytes=manifest,
                    signature_bytes=sign_for_test(manifest),
                    summary_bytes=summary,
                    installer_source=installer,
                )

    def test_manifest_requires_schema_version_integer_one(self):
        for invalid in (None, True, 0, 2, "1"):
            with self.subTest(schema_version=invalid):
                manifest, _, summary, installer = self.release_inputs(
                    manifest_changes={"schema_version": invalid}
                )
                with self.assertRaisesRegex(
                    updates.UpdateValidationError, "schema_version"
                ):
                    self.store.publish(
                        manifest_bytes=manifest,
                        signature_bytes=sign_for_test(manifest),
                        summary_bytes=summary,
                        installer_source=installer,
                    )

    def test_store_and_public_key_must_be_outside_application_source(self):
        application_source = MODULE_PATH.parents[3]
        for unsafe_root in (
            application_source,
            application_source / "unsafe-store",
            application_source.parent,
        ):
            with self.subTest(unsafe_root=unsafe_root), self.assertRaisesRegex(
                updates.UpdateValidationError, "application source"
            ):
                updates.ReleaseStore(unsafe_root, TEST_PUBLIC_KEY)
        with self.assertRaisesRegex(
            updates.UpdateValidationError, "application source"
        ):
            updates.ReleaseStore(
                self.root,
                public_key_path=application_source / "unsafe-public.pem",
            )

    def test_broad_store_roots_are_rejected_without_mutation(self):
        candidates = (Path(Path.home().anchor), Path.home().parent, Path.home())
        for candidate in candidates:
            before_mode = stat.S_IMODE(candidate.stat().st_mode)
            marker_existed = (candidate / updates.STORE_MARKER_FILENAME).exists()
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                updates.UpdateValidationError, "filesystem root|user home"
            ):
                updates.ReleaseStore(candidate, TEST_PUBLIC_KEY)
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), before_mode)
            self.assertEqual(
                (candidate / updates.STORE_MARKER_FILENAME).exists(), marker_existed
            )

    def test_existing_root_requires_private_empty_directory_or_valid_marker(self):
        manifest, signature, summary, installer = self.release_inputs()

        nonempty = self.base / "unrelated"
        nonempty.mkdir(mode=0o700)
        nonempty.chmod(0o700)
        sentinel = nonempty / "keep.txt"
        sentinel.write_text("do not touch")
        unrelated_store = updates.ReleaseStore(nonempty, TEST_PUBLIC_KEY)
        with self.assertRaisesRegex(
            updates.UpdateValidationError, "dedicated store"
        ):
            unrelated_store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )
        self.assertEqual(sentinel.read_text(), "do not touch")
        self.assertEqual(stat.S_IMODE(nonempty.stat().st_mode), 0o700)
        self.assertFalse((nonempty / updates.STORE_MARKER_FILENAME).exists())

        insecure = self.base / "insecure-empty"
        insecure.mkdir(mode=0o755)
        insecure.chmod(0o755)
        insecure_store = updates.ReleaseStore(insecure, TEST_PUBLIC_KEY)
        with self.assertRaisesRegex(updates.UpdateValidationError, "0700"):
            insecure_store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )
        self.assertEqual(stat.S_IMODE(insecure.stat().st_mode), 0o755)
        self.assertFalse((insecure / updates.STORE_MARKER_FILENAME).exists())

        safe_empty = self.base / "safe-empty"
        safe_empty.mkdir(mode=0o700)
        safe_empty.chmod(0o700)
        safe_store = updates.ReleaseStore(safe_empty, TEST_PUBLIC_KEY)
        published = safe_store.publish(
            manifest_bytes=manifest,
            signature_bytes=signature,
            summary_bytes=summary,
            installer_source=installer,
        )
        self.assertTrue(published.changed)
        self.assertEqual(
            (safe_empty / updates.STORE_MARKER_FILENAME).read_bytes(),
            updates.STORE_MARKER_BYTES,
        )

    def test_existing_store_marker_and_root_permissions_are_revalidated(self):
        self.publish("1.0.0")
        marker = self.root / updates.STORE_MARKER_FILENAME
        marker.write_bytes(b"not the update store marker\n")
        with self.assertRaisesRegex(updates.UpdateValidationError, "marker"):
            self.store.current_release()

        marker.write_bytes(updates.STORE_MARKER_BYTES)
        marker.chmod(0o600)
        self.root.chmod(0o755)
        manifest, signature, summary, installer = self.release_inputs("2.0.0")
        with self.assertRaisesRegex(updates.UpdateValidationError, "0700"):
            self.store.publish(
                manifest_bytes=manifest,
                signature_bytes=signature,
                summary_bytes=summary,
                installer_source=installer,
            )
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual((self.root / "current-version").read_text(), "1.0.0\n")


if __name__ == "__main__":
    unittest.main()

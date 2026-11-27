"""Unit tests for t3clean.verifier covering text, ASAR streaming, and option flags."""

from pathlib import Path
import tempfile
import unittest

from t3clean.verifier import (
    Finding,
    VerifyOptions,
    VerifyReport,
    _VERIFY_CHUNK_SIZE,
    _scan_binary_file,
    verify_artifact,
)


class TestVerifyArtifactText(unittest.TestCase):
    def test_clean_directory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bundle.js").write_text(
                'console.log("hello"); const prefix = "pk_live_";\n',
                encoding="utf-8",
            )
            (root / "config.json").write_text('{"ok": true}\n', encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config.txt").write_text("ingest.t3.codes", encoding="utf-8")
            (root / "bundle.js.map").write_text("ingest.t3.codes", encoding="utf-8")
            (root / "AnalyticsService.test.ts").write_text(
                'T3CODE_POSTHOG_KEY: "phc_test_key"', encoding="utf-8"
            )

            report = verify_artifact(root)
            self.assertIsInstance(report, VerifyReport)
            self.assertTrue(report.ok)
            self.assertEqual(report.findings, [])
            self.assertEqual(report.scanned_files, 2)

    def test_detects_all_forbidden_patterns_in_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "leaky.mjs").write_text(
                "\n".join(
                    [
                        "const host = 'https://ingest.t3.codes/batch/';",
                        "const key = 'phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m';",
                        "const hdr = 'X-Axiom-Dataset';",
                        "const env1 = process.env.T3CODE_POSTHOG_KEY;",
                        "const env2 = process.env.T3CODE_POSTHOG_HOST;",
                        "const env3 = process.env.T3CODE_TELEMETRY_KEY;",
                        "const env4 = process.env.T3CODE_TELEMETRY_HOST;",
                        "const dev = 't3code:analytics-device-id';",
                        "const clerkLive = 'pk_live_Y2xlcmsudDMuY29kZXMk';",
                        "const clerkTest = 'pk_test_1234567890ab';",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = verify_artifact(root)
            self.assertFalse(report.ok)
            self.assertEqual(report.scanned_files, 1)
            rules = {f.rule for f in report.findings}
            self.assertEqual(
                rules,
                {
                    "posthog-ingest-host",
                    "posthog-api-key",
                    "axiom-dataset-header",
                    "posthog-key-env",
                    "posthog-host-env",
                    "telemetry-key-env",
                    "telemetry-host-env",
                    "analytics-device-id",
                    "clerk-publishable-key",
                },
            )
            self.assertTrue(all(isinstance(f, Finding) for f in report.findings))
            clerk_finding = next(f for f in report.findings if f.rule == "clerk-publishable-key")
            self.assertIn("2 occurrence(s)", clerk_finding.detail)

    def test_clerk_regex_ignores_short_and_prefix_only_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "safe.ts"
            path.write_text(
                'const a = "pk_live_"; const b = "pk_test_short";\n',
                encoding="utf-8",
            )
            report = verify_artifact(path)
            self.assertTrue(report.ok)
            self.assertEqual(report.scanned_files, 1)

    def test_app_update_yml_flag_and_keep_updater(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            update_yml = root / "resources" / "app-update.yml"
            update_yml.parent.mkdir(parents=True)
            update_yml.write_text("provider: github\nowner: pingdotgg\n", encoding="utf-8")

            report = verify_artifact(root)
            self.assertFalse(report.ok)
            self.assertEqual([f.rule for f in report.findings], ["app-update-yml"])
            self.assertEqual(report.findings[0].path, "resources/app-update.yml")

            kept_report = verify_artifact(root, VerifyOptions(keep_updater=True))
            self.assertTrue(kept_report.ok)

            # Direct single-file check on app-update.yml
            single_report = verify_artifact(update_yml)
            self.assertFalse(single_report.ok)
            self.assertEqual(single_report.findings[0].rule, "app-update-yml")

    def test_strict_offline_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.js").write_text(
                (
                    'const m = "https://raw.githubusercontent.com/pingdotgg/t3code/main/m.json";\n'
                    'const r = "https://raw.githubusercontent.com/BerriAI/litellm/main/p.json";\n'
                ),
                encoding="utf-8",
            )

            default_report = verify_artifact(root)
            self.assertTrue(default_report.ok)

            strict_report = verify_artifact(root, VerifyOptions(strict_offline=True))
            self.assertFalse(strict_report.ok)
            self.assertEqual(
                {f.rule for f in strict_report.findings},
                {"strict-offline-model-manifest", "strict-offline-litellm-rates"},
            )

    def test_missing_target_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            verify_artifact(Path("/tmp/does-not-exist-t3clean-verify-xyz"))


class TestVerifyArtifactBinaryAsar(unittest.TestCase):
    def test_clean_and_dirty_asar_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asar_path = root / "resources" / "app.asar"
            asar_path.parent.mkdir(parents=True)

            asar_path.write_bytes(b"\x04\x00\x00\x00" + b"\x00" * 512 + b"clean-bundle-payload")
            clean_report = verify_artifact(root)
            self.assertTrue(clean_report.ok)
            self.assertEqual(clean_report.scanned_files, 1)

            dirty_payload = (
                b"\x04\x00\x00\x00"
                + b"\x00" * 200
                + b"https://ingest.t3.codes/batch/"
                + b"\xff" * 100
                + b"X-Axiom-Dataset"
                + b"\x00" * 50
                + b"pk_live_AbCdEfGhIjKlMnOpQr"
            )
            asar_path.write_bytes(dirty_payload)
            dirty_report = verify_artifact(root)
            self.assertFalse(dirty_report.ok)
            self.assertEqual(
                {f.rule for f in dirty_report.findings},
                {"posthog-ingest-host", "axiom-dataset-header", "clerk-publishable-key"},
            )
            self.assertTrue(all(f.path == "resources/app.asar" for f in dirty_report.findings))

    def test_streaming_binary_scan_handles_chunk_boundary_straddle_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asar_path = Path(tmp) / "boundary.asar"
            # Place one literal straddling a tiny 64-byte chunk boundary and one Clerk key straddling 128.
            prefix = b"A" * 58
            needle = b"ingest.t3.codes"  # spans bytes 58..73 across 64-byte boundary
            mid = b"B" * (120 - len(prefix) - len(needle))
            clerk = b"pk_test_0123456789abcdef"  # spans bytes 120..144 across 128-byte boundary
            asar_path.write_bytes(prefix + needle + mid + clerk + b"C" * 40)

            findings = _scan_binary_file(
                asar_path,
                "boundary.asar",
                literals=(("posthog-ingest-host", "ingest.t3.codes"),),
                chunk_size=64,
                overlap=32,
            )
            by_rule = {f.rule: f for f in findings}
            self.assertEqual(set(by_rule), {"posthog-ingest-host", "clerk-publishable-key"})
            self.assertIn("byte offset 58 (1 occurrence(s))", by_rule["posthog-ingest-host"].detail)
            self.assertIn(
                "pk_test_0123456789abcdef",
                by_rule["clerk-publishable-key"].detail,
            )
            self.assertIn(
                "byte offset 120 (1 occurrence(s))",
                by_rule["clerk-publishable-key"].detail,
            )

    def test_real_1mib_chunk_boundary_in_asar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            asar_path = Path(tmp) / "large.asar"
            boundary_prefix = b"\x00" * (_VERIFY_CHUNK_SIZE - 6)
            payload = boundary_prefix + b"ingest.t3.codes" + b"\x00" * 1024
            asar_path.write_bytes(payload)

            report = verify_artifact(asar_path)
            self.assertFalse(report.ok)
            self.assertEqual(len(report.findings), 1)
            self.assertEqual(report.findings[0].rule, "posthog-ingest-host")
            self.assertIn(
                f"byte offset {_VERIFY_CHUNK_SIZE - 6} (1 occurrence(s))",
                report.findings[0].detail,
            )

    def test_single_extensionless_binary_file_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sea_bin = Path(tmp) / "t3-server"
            sea_bin.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"t3code:analytics-device-id")
            report = verify_artifact(sea_bin)
            self.assertFalse(report.ok)
            self.assertEqual(report.scanned_files, 1)
            self.assertEqual(report.findings[0].rule, "analytics-device-id")


if __name__ == "__main__":
    unittest.main()

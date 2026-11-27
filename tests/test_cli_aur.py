"""Unit & integration tests for t3clean.py CLI, patch-based AUR export, and workflows."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_SCRIPT = REPO_ROOT / "t3clean.py"
AUR_DIR = REPO_ROOT / "aur"
AUR_PKGBUILD = AUR_DIR / "PKGBUILD"
AUR_SRCINFO = AUR_DIR / ".SRCINFO"
WORKFLOW_UPDATE = REPO_ROOT / ".github" / "workflows" / "update-check.yml"
WORKFLOW_SYNC = REPO_ROOT / ".github" / "workflows" / "sync-master.yml"


def _write_mock_upstream_tree(root: Path, *, vp_version: str | None = "1.8.0") -> None:
    """Create a minimal synthetic T3 Code v0.1.12+ source tree matching real anchors."""
    files = {
        "package.json": json.dumps({"name": "t3code", "engines": {"node": "26.13.1"}}),
        "apps/server/src/telemetry/AnalyticsService.ts": (
            'import * as Context from "effect/Context";\n'
            'import * as Effect from "effect/Effect";\n'
            'import * as Layer from "effect/Layer";\n'
            'import type { TelemetryOwner } from "./Identify.ts";\n'
            'const TelemetryEnvConfig = {\n'
            '  posthogKey: "phc_XOWci4oZP4VvLiEyrFqkFjP4CZn55mjYYBMREK5Wd6m",\n'
            '  posthogHost: "https://ingest.t3.codes",\n'
            '  envKey: "T3CODE_POSTHOG_KEY",\n'
            '  envHost: "T3CODE_POSTHOG_HOST",\n'
            '};\n'
            'export class AnalyticsService extends Context.Service<\n'
            '  AnalyticsService,\n'
            '  {\n'
            '    readonly record: (\n'
            '      event: string,\n'
            '      properties?: Readonly<Record<string, unknown>>,\n'
            '      options?: { readonly anonymous?: boolean },\n'
            '    ) => Effect.Effect<boolean>;\n'
            '    readonly flush: Effect.Effect<void>;\n'
            '    readonly recordOwners: (\n'
            '      owners: ReadonlyArray<TelemetryOwner>,\n'
            '      observedBy: "server" | "client",\n'
            '    ) => Effect.Effect<void>;\n'
            '  }\n'
            '>()("t3/telemetry/AnalyticsService") {}\n'
            'export const make = Effect.sync(() => AnalyticsService.of({\n'
            '  record: () => Effect.succeed(true),\n'
            '  flush: Effect.void,\n'
            '  recordOwners: () => Effect.void,\n'
            '}));\n'
            'export const layer = Layer.effect(AnalyticsService, make);\n'
        ),
        "apps/server/src/telemetry/Identify.ts": (
            'import * as Effect from "effect/Effect";\n'
            'import * as Schema from "effect/Schema";\n'
            'import * as ServerSecretStore from "../auth/ServerSecretStore.ts";\n'
            'export const TelemetryOwnerSource = Schema.Literals(["connect", "device"]);\n'
            'export type TelemetryOwnerSource = typeof TelemetryOwnerSource.Type;\n'
            'export interface TelemetryOwner {\n'
            '  readonly source: TelemetryOwnerSource;\n'
            '  readonly ownerId: string;\n'
            '}\n'
            'export class TelemetryAnonymousIdGenerationError extends Schema.TaggedError<TelemetryAnonymousIdGenerationError>()(\n'
            '  "TelemetryAnonymousIdGenerationError", { filePath: Schema.String, cause: Schema.Defect() }\n'
            ') {}\n'
            'export class TelemetryAnonymousIdPersistenceError extends Schema.TaggedError<TelemetryAnonymousIdPersistenceError>()(\n'
            '  "TelemetryAnonymousIdPersistenceError", { filePath: Schema.String, cause: Schema.Defect() }\n'
            ') {}\n'
            'export const getTelemetryIdentity = Effect.succeed({ distinctId: "abc", installId: "abc" });\n'
            'export const getServerOwners = Effect.succeed([] as ReadonlyArray<TelemetryOwner>);\n'
        ),
        "apps/web/src/connection/platform.ts": (
            'import * as Effect from "effect/Effect";\n'
            'import * as Option from "effect/Option";\n'
            'const ANALYTICS_DEVICE_ID_KEY = "t3code:analytics-device-id";\n'
            'const analyticsDevice = {\n'
            '  id: Effect.sync(\n'
            '    Option.liftThrowable(() => {\n'
            '      const existing = window.localStorage.getItem(ANALYTICS_DEVICE_ID_KEY);\n'
            '      if (existing) return existing;\n'
            '      return "generated";\n'
            '    }),\n'
            '  ),\n'
            '};\n'
            'export { analyticsDevice };\n'
        ),
        "packages/client-runtime/src/authorization/remote.ts": (
            'import * as Effect from "effect/Effect";\n'
            'export interface ClientOwnerParams {\n'
            '  readonly connect?: string;\n'
            '  readonly device?: string;\n'
            '}\n'
            'export const readClientOwnerParams = Effect.gen(function* () {\n'
            '  const x = { nested: true };\n'
            '  return x as ClientOwnerParams;\n'
            '});\n'
        ),
        "packages/shared/src/relayTracing.ts": (
            'import * as Context from "effect/Context";\n'
            'import * as Effect from "effect/Effect";\n'
            'import * as Layer from "effect/Layer";\n'
            'import * as Option from "effect/Option";\n'
            'import * as Tracer from "effect/Tracer";\n'
            'import { HttpClient } from "effect/http";\n'
            'import { OtlpTracer } from "effect/observability";\n'
            'export interface RelayClientTracingConfig {\n'
            '  readonly tracesUrl: string;\n'
            '  readonly tracesDataset: string;\n'
            '  readonly tracesToken: string;\n'
            '}\n'
            'export interface RelayClientTracingResource {\n'
            '  readonly serviceName: string;\n'
            '  readonly runtime: string;\n'
            '  readonly serviceInstanceId: string;\n'
            '  readonly client: string;\n'
            '}\n'
            'export class RelayClientTracer extends Context.Reference(\n'
            '  "@t3tools/shared/relayTracing/RelayClientTracer",\n'
            '  { defaultValue: () => Option.none<Tracer.Tracer>() },\n'
            ') {}\n'
            'export const withRelayClientTracing = <A, E, R>(effect: Effect.Effect<A, E, R>) => effect;\n'
            'export const withLocalTracing = <A, E, R>(effect: Effect.Effect<A, E, R>) => effect;\n'
            'export function nonInterferingTracer(delegate: Tracer.Tracer): Tracer.Tracer { return delegate; }\n'
            'export function layer(config: RelayClientTracingConfig | null, _resource: RelayClientTracingResource) {\n'
            '  if (!config) return Layer.succeed(RelayClientTracer, Option.none());\n'
            '  return OtlpTracer.layer({ url: config.tracesUrl, headers: { "X-Axiom-Dataset": config.tracesDataset } });\n'
            '}\n'
        ),
        "apps/web/src/index.css": (
            '@layer theme, base, clerk, components, utilities;\n'
            '@import "tailwindcss";\n'
        ),
        "scripts/linux-glibc.ts": (
            'const GLIBC_BUILD_CEILING = "2.39";\n'
            'export const scanGlibcNeeds = async () => ({ minimum: GLIBC_BUILD_CEILING, elfCount: 1 });\n'
        ),
        "apps/desktop/src/updates/DesktopUpdatePolicy.ts": (
            'export interface UpdatePolicy {\n'
            '  readonly mode: string;\n'
            '  readonly feedUrl: string | undefined;\n'
            '  readonly allowNightly: boolean;\n'
            '  readonly message: string | null;\n'
            '}\n'
            'export const unmanagedUpdatePolicy: UpdatePolicy = {\n'
            '  mode: "auto",\n'
            '  feedUrl: undefined,\n'
            '  allowNightly: true,\n'
            '  message: null,\n'
            '};\n'
        ),
        "apps/web/src/cloud/publicConfig.ts": (
            'export function resolveRelayTracingConfig() {\n'
            '  return { tracesUrl: "https://example.invalid", tracesDataset: "ds", tracesToken: "tok" };\n'
            '}\n'
            'export function hasCloudPublicConfig(): boolean {\n'
            '  const key = "pk_live_1234567890abcdef";\n'
            '  return Boolean(key);\n'
            '}\n'
        ),
        "apps/server/src/cloud/publicConfig.ts": (
            'export function resolveRelayClientTracingConfig(\n'
            '  env: Readonly<Record<string, string | undefined>> = process.env,\n'
            '  fallback = { tracesUrl: "", tracesDataset: "", tracesToken: "" },\n'
            ') {\n'
            '  return env.X ? fallback : null;\n'
            '}\n'
            'export const hasCloudPublicConfig = Boolean(\n'
            '  process.env.T3CODE_RELAY_URL && process.env.T3CODE_CLERK_PUBLISHABLE_KEY,\n'
            ');\n'
        ),
        # Rules 7 & 8 — MUST remain untouched
        "apps/server/src/provider/ModelManifest.ts": (
            'export const MANIFEST_URL = "https://raw.githubusercontent.com/pingdotgg/t3code/main/models.json";\n'
        ),
        "apps/server/src/usage/UsageService.ts": (
            'export const LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices.json";\n'
        ),
    }
    if vp_version is not None:
        files["pnpm-workspace.yaml"] = (
            f"catalog:\n  vite-plus: {vp_version}\noverrides:\n  vite-plus: 'catalog:'\n"
        )

    for rel_path, content in files.items():
        full_path = root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


def _build_mock_tarball(tarball_path: Path, stem: str = "t3code-0.1.12", vp_version: str | None = "1.8.0") -> None:
    with tempfile.TemporaryDirectory(prefix="t3clean-tar-src-") as tmp:
        tree = Path(tmp) / stem
        _write_mock_upstream_tree(tree, vp_version=vp_version)
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(tree, arcname=stem)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestCliAndAur(unittest.TestCase):
    def test_cli_executable_bit(self) -> None:
        mode = CLI_SCRIPT.stat().st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR), f"Expected executable bit on {CLI_SCRIPT}")

    def test_cli_help_all_subcommands(self) -> None:
        subcommands = [
            [],
            ["fetch"],
            ["latest-release"],
            ["patch"],
            ["verify"],
            ["build"],
            ["deploy-server"],
            ["rollback-server"],
            ["export-aur"],
        ]
        for subcmd in subcommands:
            proc = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), *subcmd, "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"--help failed for {subcmd}: {proc.stderr}")
            self.assertIn("usage:", proc.stdout.lower())

        aur_help = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "export-aur", "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for flag in ("--source", "--source-tarball", "--source-sha256", "--ref", "--pkgrel", "--skip-verify"):
            self.assertIn(flag, aur_help)

    def test_cli_patch_and_verify_e2e(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-cli-test-") as tmp:
            src_dir = Path(tmp) / "src"
            _write_mock_upstream_tree(src_dir)
            model_before = (src_dir / "apps/server/src/provider/ModelManifest.ts").read_text(encoding="utf-8")
            usage_before = (src_dir / "apps/server/src/usage/UsageService.ts").read_text(encoding="utf-8")

            pre_verify = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "verify", "--artifact", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pre_verify.returncode, 1)
            self.assertFalse(json.loads(pre_verify.stdout)["ok"])

            dry_proc = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "patch", "--source", str(src_dir), "--dry-run", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(dry_proc.returncode, 0, dry_proc.stderr)
            self.assertIn(
                "ingest.t3.codes",
                (src_dir / "apps/server/src/telemetry/AnalyticsService.ts").read_text(encoding="utf-8"),
            )

            patch1 = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "patch", "--source", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(patch1.returncode, 0, patch1.stderr)
            self.assertTrue(any(r["changed"] for r in json.loads(patch1.stdout)["results"]))

            patch2 = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "patch", "--source", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(patch2.returncode, 0, patch2.stderr)
            p2_data = json.loads(patch2.stdout)
            self.assertTrue(all(not r["changed"] and r["already_patched"] for r in p2_data["results"]))

            post_verify = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "verify", "--artifact", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(post_verify.returncode, 0, post_verify.stdout + post_verify.stderr)
            self.assertEqual(json.loads(post_verify.stdout)["findings"], [])

            strict_verify = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "verify", "--artifact", str(src_dir), "--strict-offline", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(strict_verify.returncode, 1)

            self.assertEqual(
                (src_dir / "apps/server/src/provider/ModelManifest.ts").read_text(encoding="utf-8"),
                model_before,
            )
            self.assertEqual(
                (src_dir / "apps/server/src/usage/UsageService.ts").read_text(encoding="utf-8"),
                usage_before,
            )

    def test_cli_rollback_list_empty_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-rollback-list-") as tmp:
            proc = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "rollback-server", "--t3-home", tmp, "--list", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertTrue(data["ok"])
            self.assertEqual(data["backups"], [])

    def test_checked_in_aur_directory_and_workflows_are_valid(self) -> None:
        expected_aur_files = [
            ".SRCINFO",
            ".gitignore",
            "0001-strip-telemetry.patch",
            "0002-linux-build-compat.patch",
            "PKGBUILD",
        ]
        for fname in expected_aur_files:
            self.assertTrue((AUR_DIR / fname).is_file(), f"Missing checked-in AUR file: aur/{fname}")
        for retired in ("t3clean-standalone.py", "t3-code-no-telemetry.sh", "t3-code-no-telemetry.desktop"):
            self.assertFalse((AUR_DIR / retired).exists())

        bash_check = subprocess.run(
            ["bash", "-n", str(AUR_PKGBUILD)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

        pkgbuild = AUR_PKGBUILD.read_text(encoding="utf-8")
        srcinfo = AUR_SRCINFO.read_text(encoding="utf-8")
        self.assertNotIn("../", pkgbuild)
        self.assertNotIn("_build_deb", pkgbuild)
        self.assertNotIn("t3clean-standalone.py", pkgbuild)
        self.assertNotIn("'SKIP'", pkgbuild)
        self.assertNotIn("\tsha256sums = SKIP", srcinfo)
        self.assertIn("patch -Np1 -i", pkgbuild)
        self.assertIn("install -Dm755 /dev/stdin", pkgbuild)
        self.assertIn("install -Dm644 /dev/stdin", pkgbuild)
        self.assertRegex(
            pkgbuild,
            re.compile(r"^_vp_version='\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?'$", re.MULTILINE),
        )

        # Patch checksums in aur/PKGBUILD and aur/.SRCINFO must match on-disk patch files.
        pkg_sums = re.findall(r"'([0-9a-f]{64})'", re.search(r"sha256sums=\(([^)]+)\)", pkgbuild, re.DOTALL).group(1))
        src_sums = re.findall(r"^\tsha256sums = ([0-9a-f]{64})$", srcinfo, re.MULTILINE)
        self.assertEqual(len(pkg_sums), 3)
        self.assertEqual(pkg_sums, src_sums)
        self.assertEqual(pkg_sums[1], _sha256(AUR_DIR / "0001-strip-telemetry.patch"))
        self.assertEqual(pkg_sums[2], _sha256(AUR_DIR / "0002-linux-build-compat.patch"))

        # makepkg refuses to execute as root (exit 10); skip only under uid 0.
        if shutil.which("makepkg") and (not hasattr(os, "geteuid") or os.geteuid() != 0):
            mk = subprocess.run(
                ["makepkg", "--printsrcinfo"],
                cwd=str(AUR_DIR),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(mk.returncode, 0, mk.stderr)
            self.assertEqual(srcinfo, mk.stdout)

        # Verify GitHub Actions workflows exist and reference expected branches/files.
        update_yml = WORKFLOW_UPDATE.read_text(encoding="utf-8")
        sync_yml = WORKFLOW_SYNC.read_text(encoding="utf-8")
        self.assertIn("latest-release", update_yml)
        self.assertIn("peter-evans/create-pull-request", update_yml)
        self.assertIn("Automated de-telemetry patch failed", update_yml)
        self.assertIn("0001-strip-telemetry.patch", sync_yml)
        self.assertIn("0002-linux-build-compat.patch", sync_yml)
        self.assertIn("gh release create", sync_yml)

    def test_export_aur_from_tarball_source_and_download_modes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-aur-export-") as tmp:
            root = Path(tmp)

            # 1. --source-tarball mode (zero network, real SHA-256, cleans obsolete files)
            tarball_path = root / "t3code-v0.1.12.tar.gz"
            _build_mock_tarball(tarball_path, stem="t3code-0.1.12", vp_version="1.8.0")
            tarball_sha = _sha256(tarball_path)

            out_tar = root / "aur-tarball"
            out_tar.mkdir()
            (out_tar / "t3clean-standalone.py").write_text("# stale\n", encoding="utf-8")

            proc_tar = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_tar),
                    "--ref",
                    "v0.1.12",
                    "--pkgrel",
                    "2",
                    "--source-tarball",
                    str(tarball_path),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_tar.returncode, 0, proc_tar.stdout + proc_tar.stderr)
            payload = json.loads(proc_tar.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["source_sha256"], tarball_sha)
            self.assertEqual(payload["vp_version"], "1.8.0")
            self.assertGreater(payload["verified_files"], 0)
            self.assertIn("apps/server/src/telemetry/AnalyticsService.ts", payload["audit_files"])

            entries = sorted(p.name for p in out_tar.iterdir())
            self.assertEqual(
                entries,
                [
                    ".SRCINFO",
                    ".gitignore",
                    "0001-strip-telemetry.patch",
                    "0002-linux-build-compat.patch",
                    "PKGBUILD",
                ],
            )
            pkgbuild = (out_tar / "PKGBUILD").read_text(encoding="utf-8")
            srcinfo = (out_tar / ".SRCINFO").read_text(encoding="utf-8")
            expected_sums = [
                tarball_sha,
                _sha256(out_tar / "0001-strip-telemetry.patch"),
                _sha256(out_tar / "0002-linux-build-compat.patch"),
            ]
            pkg_sums = re.findall(r"'([^']+)'", re.search(r"sha256sums=\(([^)]+)\)", pkgbuild, re.DOTALL).group(1))
            src_sums = re.findall(r"^\tsha256sums = (.+)$", srcinfo, re.MULTILINE)
            self.assertEqual(pkg_sums, expected_sums)
            self.assertEqual(src_sums, expected_sums)
            self.assertIn("pkgrel=2", pkgbuild)
            self.assertIn("_vp_version='1.8.0'", pkgbuild)

            # 2. --source mode with explicit uppercase --source-sha256 (does not mutate source)
            src_dir = root / "clean-src"
            _write_mock_upstream_tree(src_dir, vp_version=None)
            before_analytics = (src_dir / "apps/server/src/telemetry/AnalyticsService.ts").read_text(encoding="utf-8")
            explicit_upper = "A1B2C3D4" * 8
            out_src = root / "aur-src"
            proc_src = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_src),
                    "--ref",
                    "v0.1.13-nightly.20261125",
                    "--source",
                    str(src_dir),
                    "--source-sha256",
                    explicit_upper,
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_src.returncode, 0, proc_src.stdout + proc_src.stderr)
            src_payload = json.loads(proc_src.stdout)
            self.assertEqual(src_payload["source_sha256"], explicit_upper.lower())
            self.assertIsNone(src_payload["vp_version"])
            self.assertIn("_vp_version=''", (out_src / "PKGBUILD").read_text(encoding="utf-8"))
            self.assertEqual(
                (src_dir / "apps/server/src/telemetry/AnalyticsService.ts").read_text(encoding="utf-8"),
                before_analytics,
            )

            # 3. Default download mode with --ref latest-release (mocked download & release API)
            import t3clean.aur as aur_mod

            out_auto = root / "aur-auto"
            spec = importlib.util.spec_from_file_location("t3clean_cli_aur_auto", CLI_SCRIPT)
            assert spec is not None and spec.loader is not None
            cli_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli_mod)

            def fake_download(url: str, dest: Path) -> None:
                self.assertTrue(url.endswith("/archive/refs/tags/v0.1.13.tar.gz"))
                _build_mock_tarball(dest, stem="t3code-0.1.13", vp_version="1.9.0")

            with (
                mock.patch.object(
                    aur_mod,
                    "resolve_latest_github_release",
                    return_value={"tag": "v0.1.13", "version": "0.1.13", "html_url": "", "published_at": ""},
                ),
                mock.patch.object(aur_mod, "_download_to_file", side_effect=fake_download),
            ):
                rc = cli_mod.main(["export-aur", "--out", str(out_auto), "--ref", "latest-release"])
            self.assertEqual(rc, 0)
            auto_pkgbuild = (out_auto / "PKGBUILD").read_text(encoding="utf-8")
            self.assertIn("pkgver=0.1.13", auto_pkgbuild)
            self.assertIn("_vp_version='1.9.0'", auto_pkgbuild)
            self.assertNotIn("'SKIP'", auto_pkgbuild)

            # 4. Telemetry leak in unpatched extra file fails export-aur loudly!
            dirty_src = root / "dirty-src"
            _write_mock_upstream_tree(dirty_src)
            (dirty_src / "apps/server/src/telemetry/NewLeak.ts").write_text(
                'export const endpoint = "https://ingest.t3.codes";\n',
                encoding="utf-8",
            )
            proc_leak = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(root / "aur-leak"),
                    "--source",
                    str(dirty_src),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_leak.returncode, 1)
            self.assertIn("failed telemetry verification", json.loads(proc_leak.stdout)["error"])

    def test_exported_pkgbuild_vp_helpers_and_inline_check_execute_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-aur-bash-") as tmp:
            root = Path(tmp)
            src_dir = root / "src"
            _write_mock_upstream_tree(src_dir, vp_version="1.8.0")
            out_dir = root / "aur-out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_dir),
                    "--ref",
                    "v0.1.12",
                    "--source",
                    str(src_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            pkgbuild_path = out_dir / "PKGBUILD"

            # Fake host PATH with breaking vp 2.0.0 + recording fake npm
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            npm_log = root / "npm.log"
            (fake_bin / "vp").write_text("#!/bin/sh\necho 'vp v2.0.0'\n", encoding="utf-8")
            (fake_bin / "vp").chmod(0o755)
            (fake_bin / "npm").write_text(
                f"#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{npm_log}"\n'
                f'prefix=""\n'
                f'spec=""\n'
                f'while [ $# -gt 0 ]; do\n'
                f'  if [ "$1" = "--prefix" ]; then shift; prefix="$1"; else spec="$1"; fi\n'
                f'  shift\n'
                f'done\n'
                f'mkdir -p "$prefix/bin"\n'
                f'ver="${{spec#*@}}"\n'
                f'ver="${{ver#^}}"\n'
                f'printf "#!/bin/sh\\necho \\"vp v%s\\"\\n" "$ver" > "$prefix/bin/vp"\n'
                f'chmod +x "$prefix/bin/vp"\n',
                encoding="utf-8",
            )
            (fake_bin / "npm").chmod(0o755)

            # Prepare clean and dirty fake build outputs for check()
            fake_srcdir = root / "srcdir"
            unpacked = fake_srcdir / "out" / "linux-unpacked" / "resources"
            unpacked.mkdir(parents=True)
            (unpacked / "app.asar").write_bytes(b"clean binary payload without trackers")

            cache_dir = root / "cache"
            harness = (
                "set -euo pipefail\n"
                f'source "{pkgbuild_path}"\n'
                f'export T3CLEAN_CACHE_DIR="{cache_dir}"\n'
                f'export PATH="{fake_bin}:$PATH"\n'
                f'! _t3clean_vp_ok "{fake_bin}/vp" "1.8.0"\n'
                f'_t3clean_ensure_vp\n'
                f'test "$(command -v vp)" = "{cache_dir}/toolchains/vp-isolated-v1.8.0/bin/vp"\n'
                f'srcdir="{fake_srcdir}"\n'
                f'check\n'
            )
            bash_ok = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=False)
            self.assertEqual(bash_ok.returncode, 0, bash_ok.stdout + bash_ok.stderr)
            self.assertIn("vite-plus@1.8.0", npm_log.read_text(encoding="utf-8"))

            # Dirty app.asar must cause inline check() to fail with nonzero exit!
            (unpacked / "app.asar").write_bytes(b"prefix https://ingest.t3.codes suffix")
            dirty_harness = (
                "set -euo pipefail\n"
                f'source "{pkgbuild_path}"\n'
                f'srcdir="{fake_srcdir}"\n'
                f'check\n'
            )
            bash_fail = subprocess.run(["bash", "-c", dirty_harness], capture_output=True, text=True, check=False)
            self.assertEqual(bash_fail.returncode, 1)
            self.assertIn("posthog-ingest-host", bash_fail.stderr)

    def test_cli_build_propagates_fetched_version(self) -> None:
        spec = importlib.util.spec_from_file_location("t3clean_cli_under_test", CLI_SCRIPT)
        assert spec is not None and spec.loader is not None
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)

        with tempfile.TemporaryDirectory(prefix="t3clean-build-ver-") as tmp:
            src_dir = Path(tmp) / "src"
            src_dir.mkdir()
            captured: list[object] = []

            def fake_build_target(opts: object) -> Path:
                captured.append(opts)
                return Path(tmp) / "out" / "linux-unpacked"

            fake_fetch = mock.MagicMock(
                return_value={
                    "ref": "v0.1.12",
                    "resolved_ref": "v0.1.12",
                    "commit": "deadbeef",
                    "version": "0.1.12+notelemetry",
                }
            )
            fake_patch = mock.MagicMock(return_value=[])

            with (
                mock.patch.object(cli_mod, "fetch_source", fake_fetch),
                mock.patch.object(cli_mod, "patch_source_tree", fake_patch),
                mock.patch.object(cli_mod, "build_target", side_effect=fake_build_target),
            ):
                rc = cli_mod.main(["build", "--source", str(src_dir), "--ref", "v0.1.12"])
                self.assertEqual(rc, 0)
                self.assertEqual(captured[-1].version, "0.1.12+notelemetry")

                rc = cli_mod.main(
                    ["build", "--source", str(src_dir), "--ref", "v0.1.12", "--version", "0.1.12+custom"]
                )
                self.assertEqual(rc, 0)
                self.assertEqual(captured[-1].version, "0.1.12+custom")

                fake_fetch.reset_mock()
                rc = cli_mod.main(["build", "--source", str(src_dir), "--skip-fetch", "--jobs", "3"])
                self.assertEqual(rc, 0)
                fake_fetch.assert_not_called()
                self.assertIsNone(captured[-1].version)
                self.assertEqual(captured[-1].jobs, 3)


if __name__ == "__main__":
    unittest.main()

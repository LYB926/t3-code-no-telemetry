"""Unit & integration tests for t3clean.py CLI, AUR flat export, and Arch packaging."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_SCRIPT = REPO_ROOT / "t3clean.py"
ARCH_DIR = REPO_ROOT / "packaging" / "arch"
ARCH_PKGBUILD = ARCH_DIR / "PKGBUILD"
ARCH_LAUNCHER = ARCH_DIR / "t3-code-no-telemetry.sh"
ARCH_DESKTOP = ARCH_DIR / "t3-code-no-telemetry.desktop"


def _write_mock_upstream_tree(root: Path) -> None:
    """Create a minimal synthetic T3 Code v0.1.12 source tree matching real anchors."""
    files = {
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

    for rel_path, content in files.items():
        full_path = root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestCliAndAur(unittest.TestCase):
    def test_executables_have_exec_bits(self) -> None:
        for script in (CLI_SCRIPT, ARCH_LAUNCHER):
            mode = script.stat().st_mode
            self.assertTrue(
                bool(mode & stat.S_IXUSR),
                f"Expected executable permission bit on {script}",
            )

    def test_cli_help_all_subcommands(self) -> None:
        subcommands = [
            [],
            ["fetch"],
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
            self.assertEqual(
                proc.returncode,
                0,
                f"--help failed for {subcmd}: {proc.stderr}",
            )
            self.assertIn("usage:", proc.stdout.lower())

        # Verify key flags exist on build & deploy-server help output
        build_help = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "build", "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for flag in (
            "--bootstrap-toolchain",
            "--jobs",
            "--use-system-electron",
            "--system-electron-bin",
            "--cache-dir",
            "--strict-offline",
            "desktop-dir",
            "server-sea",
            "server-bundle",
        ):
            self.assertIn(flag, build_help)

        aur_help = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "export-aur", "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for flag in ("--source-sha256", "--source-tarball", "--ref", "--pkgrel"):
            self.assertIn(flag, aur_help)

    def test_cli_patch_and_verify_e2e(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-cli-test-") as tmp:
            src_dir = Path(tmp) / "src"
            _write_mock_upstream_tree(src_dir)
            model_before = (src_dir / "apps/server/src/provider/ModelManifest.ts").read_text(encoding="utf-8")
            usage_before = (src_dir / "apps/server/src/usage/UsageService.ts").read_text(encoding="utf-8")

            # Verify fails before patching
            pre_verify = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "verify", "--artifact", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pre_verify.returncode, 1)
            pre_data = json.loads(pre_verify.stdout)
            self.assertFalse(pre_data["ok"])
            self.assertGreater(len(pre_data["findings"]), 0)

            # Dry-run leaves tree unchanged
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

            # Real patch pass #1
            patch1 = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "patch", "--source", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(patch1.returncode, 0, patch1.stderr)
            p1_data = json.loads(patch1.stdout)
            self.assertTrue(p1_data["ok"])
            self.assertTrue(any(r["changed"] for r in p1_data["results"]))

            # Real patch pass #2 (strict idempotency)
            patch2 = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "patch", "--source", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(patch2.returncode, 0, patch2.stderr)
            p2_data = json.loads(patch2.stdout)
            self.assertTrue(p2_data["ok"])
            self.assertTrue(all(not r["changed"] for r in p2_data["results"]))
            self.assertTrue(all(r["already_patched"] for r in p2_data["results"]))

            # Verify passes after patching
            post_verify = subprocess.run(
                [sys.executable, str(CLI_SCRIPT), "verify", "--artifact", str(src_dir), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(post_verify.returncode, 0, post_verify.stdout + post_verify.stderr)
            post_data = json.loads(post_verify.stdout)
            self.assertTrue(post_data["ok"])
            self.assertEqual(post_data["findings"], [])

            # Strict-offline flags preserved rules 7 & 8
            strict_verify = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "verify",
                    "--artifact",
                    str(src_dir),
                    "--strict-offline",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(strict_verify.returncode, 1)

            # Confirm rules 7 & 8 untouched
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
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "rollback-server",
                    "--t3-home",
                    tmp,
                    "--list",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertTrue(data["ok"])
            self.assertEqual(data["backups"], [])

    def test_arch_packaging_files_and_bash_syntax(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(ARCH_PKGBUILD), str(ARCH_LAUNCHER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"bash -n failed: {proc.stderr}")

        pkgbuild_text = ARCH_PKGBUILD.read_text(encoding="utf-8")
        self.assertIn("pkgname=t3-code-no-telemetry", pkgbuild_text)
        self.assertNotIn("fuse2", pkgbuild_text)
        self.assertNotIn("vite-plus>=1.4.0", pkgbuild_text)
        self.assertNotIn("git+https://", pkgbuild_text)
        self.assertIn("archive/refs/tags/", pkgbuild_text)
        self.assertIn("--bootstrap-toolchain", pkgbuild_text)
        self.assertIn('_jobs="${T3CLEAN_JOBS:-}"', pkgbuild_text)
        self.assertIn('--jobs "${_jobs}"', pkgbuild_text)
        self.assertNotIn("conflicts=('t3code-bin')", pkgbuild_text)
        self.assertNotIn('conflicts=("t3code-bin")', pkgbuild_text)
        self.assertIn('_build_deb="${T3CLEAN_BUILD_DEB:-0}"', pkgbuild_text)
        self.assertIn('_use_system_electron="${T3CLEAN_USE_SYSTEM_ELECTRON:-0}"', pkgbuild_text)
        self.assertIn('_system_electron_bin="${T3CLEAN_SYSTEM_ELECTRON_BIN:-electron}"', pkgbuild_text)
        self.assertIn("T3CLEAN_CACHE_DIR", pkgbuild_text)
        self.assertIn("${srcdir}/.cache", pkgbuild_text)
        self.assertIn("options=('!strip' '!debug')", pkgbuild_text)

        launcher_text = ARCH_LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("T3CODE_TELEMETRY_ENABLED=false", launcher_text)
        self.assertIn("DO_NOT_TRACK=1", launcher_text)
        self.assertIn("--ozone-platform=wayland", launcher_text)
        self.assertIn("--ozone-platform-hint=auto", launcher_text)
        self.assertIn("/usr/lib/t3-code-no-telemetry/.use-system-electron", launcher_text)
        self.assertIn("/usr/lib/t3-code-no-telemetry/resources/app.asar", launcher_text)
        self.assertIn("/usr/lib/t3-code-no-telemetry/t3code", launcher_text)

        desktop_text = ARCH_DESKTOP.read_text(encoding="utf-8")
        for expected in (
            "Name=T3 Code (No Telemetry)",
            "Exec=t3-code-no-telemetry %U",
            "Icon=t3-code-no-telemetry",
            "StartupWMClass=com.t3tools.T3Code",
            "Categories=Development;IDE;",
            "MimeType=x-scheme-handler/t3code;",
        ):
            self.assertIn(expected, desktop_text)

    def test_export_aur_flat_checksums_srcinfo_and_standalone_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-aur-export-") as tmp:
            out_dir = Path(tmp) / "aur-out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_dir),
                    "--ref",
                    "v0.1.12",
                    "--pkgrel",
                    "2",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["source_sha256"], "SKIP")

            # 100% flat directory: zero subdirectories, exact 5 files
            entries = sorted(out_dir.iterdir(), key=lambda p: p.name)
            self.assertTrue(all(p.is_file() for p in entries), f"Subdirectories found: {entries}")
            self.assertEqual(
                [p.name for p in entries],
                [
                    ".SRCINFO",
                    "PKGBUILD",
                    "t3-code-no-telemetry.desktop",
                    "t3-code-no-telemetry.sh",
                    "t3clean-standalone.py",
                ],
            )

            pkgbuild = (out_dir / "PKGBUILD").read_text(encoding="utf-8")
            srcinfo = (out_dir / ".SRCINFO").read_text(encoding="utf-8")

            # Zero relative parent paths in exported PKGBUILD / .SRCINFO
            self.assertNotIn("../", pkgbuild)
            self.assertNotIn("../", srcinfo)
            self.assertNotIn("fuse2", pkgbuild)
            self.assertNotIn("fuse2", srcinfo)
            self.assertNotIn("vite-plus>=1.4.0", pkgbuild)
            self.assertNotIn("vite-plus>=1.4.0", srcinfo)
            self.assertNotIn("git+https://", pkgbuild)
            self.assertIn(
                "t3code-v0.1.12.tar.gz::https://github.com/pingdotgg/t3code/archive/refs/tags/v0.1.12.tar.gz",
                pkgbuild,
            )
            self.assertIn("_t3clean_required_vp_version", pkgbuild)
            self.assertIn("_t3clean_ensure_vp", pkgbuild)
            self.assertIn("vite-plus@^1.4.0", pkgbuild)
            self.assertIn("_t3clean_apply_memory_guards", pkgbuild)
            self.assertIn("T3CLEAN_JOBS", pkgbuild)
            self.assertIn("GIT_CEILING_DIRECTORIES", pkgbuild)
            self.assertIn("T3CODE_GLIBC_BUILD_CEILING", pkgbuild)
            self.assertIn("pkgrel=2", pkgbuild)
            self.assertIn('python3 "${srcdir}/t3clean-standalone.py"', pkgbuild)

            # bash -n on exported PKGBUILD & launcher
            bash_check = subprocess.run(
                ["bash", "-n", str(out_dir / "PKGBUILD"), str(out_dir / "t3-code-no-telemetry.sh")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bash_check.returncode, 0, bash_check.stderr)

            # Validate SHA-256 sums match actual files in both PKGBUILD and .SRCINFO
            expected_sums = [
                "SKIP",
                _sha256(out_dir / "t3clean-standalone.py"),
                _sha256(out_dir / "t3-code-no-telemetry.sh"),
                _sha256(out_dir / "t3-code-no-telemetry.desktop"),
            ]
            pkgbuild_sums_match = re.search(r"sha256sums=\(([^)]+)\)", pkgbuild, re.DOTALL)
            self.assertIsNotNone(pkgbuild_sums_match)
            assert pkgbuild_sums_match is not None
            pkgbuild_sums = re.findall(r"'([^']+)'", pkgbuild_sums_match.group(1))
            self.assertEqual(pkgbuild_sums, expected_sums)

            srcinfo_sums = re.findall(r"^\tsha256sums = (.+)$", srcinfo, re.MULTILINE)
            self.assertEqual(srcinfo_sums, expected_sums)
            self.assertTrue(srcinfo.startswith("pkgbase = t3-code-no-telemetry\n"))
            self.assertIn("\tpkgver = 0.1.12\n", srcinfo)
            self.assertIn("\tpkgrel = 2\n", srcinfo)
            self.assertIn(
                "\tsource = t3code-v0.1.12.tar.gz::https://github.com/pingdotgg/t3code/archive/refs/tags/v0.1.12.tar.gz\n",
                srcinfo,
            )
            self.assertTrue(srcinfo.rstrip().endswith("pkgname = t3-code-no-telemetry"))

            # Run t3clean-standalone.py completely isolated from repo (no PYTHONPATH)
            isolated_dir = Path(tmp) / "isolated"
            isolated_dir.mkdir()
            isolated_script = isolated_dir / "t3clean-standalone.py"
            shutil.copyfile(out_dir / "t3clean-standalone.py", isolated_script)
            mock_src = isolated_dir / "mock-t3code"
            _write_mock_upstream_tree(mock_src)

            clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

            pre_v = subprocess.run(
                [sys.executable, str(isolated_script), "verify", "--artifact", str(mock_src)],
                cwd=str(isolated_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pre_v.returncode, 1)

            p_run = subprocess.run(
                [sys.executable, str(isolated_script), "patch", "--source", str(mock_src)],
                cwd=str(isolated_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_run.returncode, 0, p_run.stderr)

            p_run2 = subprocess.run(
                [sys.executable, str(isolated_script), "patch", "--source", str(mock_src), "--json"],
                cwd=str(isolated_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(p_run2.returncode, 0, p_run2.stderr)
            self.assertTrue(all(not r["changed"] for r in json.loads(p_run2.stdout)["results"]))

            post_v = subprocess.run(
                [sys.executable, str(isolated_script), "verify", "--artifact", str(mock_src)],
                cwd=str(isolated_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(post_v.returncode, 0, post_v.stdout + post_v.stderr)

    def test_export_aur_branch_and_commit_refs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-aur-branch-") as tmp:
            out_dir = Path(tmp) / "aur-main"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_dir),
                    "--ref",
                    "main",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            pkgbuild = (out_dir / "PKGBUILD").read_text(encoding="utf-8")
            srcinfo = (out_dir / ".SRCINFO").read_text(encoding="utf-8")
            expected_branch = "t3code-main.tar.gz::https://github.com/pingdotgg/t3code/archive/refs/heads/main.tar.gz"
            self.assertIn(expected_branch, pkgbuild)
            self.assertIn(expected_branch, srcinfo)

            commit_dir = Path(tmp) / "aur-sha"
            proc_sha = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(commit_dir),
                    "--ref",
                    "deadbeef1234",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_sha.returncode, 0, proc_sha.stderr)
            sha_pkgbuild = (commit_dir / "PKGBUILD").read_text(encoding="utf-8")
            sha_srcinfo = (commit_dir / ".SRCINFO").read_text(encoding="utf-8")
            expected_sha = "t3code-deadbeef1234.tar.gz::https://github.com/pingdotgg/t3code/archive/deadbeef1234.tar.gz"
            self.assertIn(expected_sha, sha_pkgbuild)
            self.assertIn(expected_sha, sha_srcinfo)

    def test_export_aur_source_sha256_explicit_tarball_auto_and_validation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-aur-sha-") as tmp:
            root = Path(tmp)

            # 1. Explicit uppercase 64-hex digest is normalized to lowercase in PKGBUILD + .SRCINFO.
            explicit_upper = "A1B2C3D4" * 8
            explicit_lower = explicit_upper.lower()
            out_explicit = root / "aur-explicit"
            proc_explicit = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_explicit),
                    "--ref",
                    "v0.1.12",
                    "--source-sha256",
                    explicit_upper,
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_explicit.returncode, 0, proc_explicit.stderr)
            explicit_payload = json.loads(proc_explicit.stdout)
            self.assertEqual(explicit_payload["source_sha256"], explicit_lower)
            pkgbuild_text = (out_explicit / "PKGBUILD").read_text(encoding="utf-8")
            srcinfo_text = (out_explicit / ".SRCINFO").read_text(encoding="utf-8")
            self.assertIn(f"  '{explicit_lower}'\n", pkgbuild_text)
            self.assertIn(f"\tsha256sums = {explicit_lower}\n", srcinfo_text)
            self.assertNotIn("'SKIP'", pkgbuild_text)
            self.assertNotIn("\tsha256sums = SKIP", srcinfo_text)

            # 2. --source-tarball hashes a local archive file directly.
            fake_tarball = root / "t3code-v0.1.12.tar.gz"
            fake_tarball.write_bytes(b"synthetic-t3code-source-tarball-v0.1.12")
            tarball_sha = _sha256(fake_tarball)
            out_tarball = root / "aur-tarball"
            proc_tarball = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(out_tarball),
                    "--ref",
                    "v0.1.12",
                    "--source-tarball",
                    str(fake_tarball),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_tarball.returncode, 0, proc_tarball.stderr)
            tarball_payload = json.loads(proc_tarball.stdout)
            self.assertEqual(tarball_payload["source_sha256"], tarball_sha)
            self.assertIn(f"  '{tarball_sha}'\n", (out_tarball / "PKGBUILD").read_text(encoding="utf-8"))
            self.assertIn(f"\tsha256sums = {tarball_sha}\n", (out_tarball / ".SRCINFO").read_text(encoding="utf-8"))

            # 3. --source-sha256 auto streams the GitHub archive URL once.
            import t3clean.aur as aur_mod

            auto_sha = "f0e1d2c3" * 8
            out_auto = root / "aur-auto"
            spec = importlib.util.spec_from_file_location("t3clean_cli_aur_auto", CLI_SCRIPT)
            assert spec is not None and spec.loader is not None
            cli_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli_mod)
            with mock.patch.object(aur_mod, "_sha256_url", return_value=auto_sha) as sha_url_mock:
                rc = cli_mod.main(
                    [
                        "export-aur",
                        "--out",
                        str(out_auto),
                        "--ref",
                        "v0.1.12",
                        "--source-sha256",
                        "auto",
                    ]
                )
            self.assertEqual(rc, 0)
            sha_url_mock.assert_called_once_with(
                "https://github.com/pingdotgg/t3code/archive/refs/tags/v0.1.12.tar.gz"
            )
            self.assertIn(f"  '{auto_sha}'\n", (out_auto / "PKGBUILD").read_text(encoding="utf-8"))
            self.assertIn(f"\tsha256sums = {auto_sha}\n", (out_auto / ".SRCINFO").read_text(encoding="utf-8"))

            # 4. Invalid digest, mutual exclusion, and missing tarball all fail cleanly.
            bad_hex = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(root / "aur-bad"),
                    "--source-sha256",
                    "not-a-valid-sha256",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bad_hex.returncode, 1)
            self.assertIn("Invalid source_sha256", json.loads(bad_hex.stdout)["error"])

            both_flags = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(root / "aur-both"),
                    "--source-sha256",
                    explicit_lower,
                    "--source-tarball",
                    str(fake_tarball),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(both_flags.returncode, 1)
            self.assertIn("Cannot specify both", json.loads(both_flags.stdout)["error"])

            missing_tar = subprocess.run(
                [
                    sys.executable,
                    str(CLI_SCRIPT),
                    "export-aur",
                    "--out",
                    str(root / "aur-missing"),
                    "--source-tarball",
                    str(root / "does-not-exist.tar.gz"),
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing_tar.returncode, 1)
            self.assertIn("Source tarball not found", json.loads(missing_tar.stdout)["error"])

    def test_export_aur_pkgbuild_vp_pinning_helpers_execute_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t3clean-aur-vp-bash-") as tmp:
            root = Path(tmp)
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
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            pkgbuild_path = out_dir / "PKGBUILD"

            # Synthetic source dirs: catalog-pinned, package.json fallback, and unpinned.
            src_catalog = root / "src-catalog"
            src_catalog.mkdir()
            (src_catalog / "pnpm-workspace.yaml").write_text(
                "catalog:\n  vite-plus: 1.8.0\noverrides:\n  vite-plus: 'catalog:'\n",
                encoding="utf-8",
            )
            src_pkg = root / "src-pkg"
            src_pkg.mkdir()
            (src_pkg / "package.json").write_text(
                json.dumps({"devDependencies": {"vite-plus": "^1.7.5"}}),
                encoding="utf-8",
            )
            src_none = root / "src-none"
            src_none.mkdir()

            # Fake host PATH containing a breaking vp 2.0.0 and a recording fake npm.
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

            cache_dir = root / "cache"
            harness = (
                "set -euo pipefail\n"
                f'source "{pkgbuild_path}"\n'
                f'export T3CLEAN_CACHE_DIR="{cache_dir}"\n'
                f'export PATH="{fake_bin}:$PATH"\n'
                f'test "$(_t3clean_required_vp_version "{src_catalog}")" = "1.8.0"\n'
                f'test "$(_t3clean_required_vp_version "{src_pkg}")" = "1.7.5"\n'
                f'test -z "$(_t3clean_required_vp_version "{src_none}")"\n'
                f'! _t3clean_vp_ok "{fake_bin}/vp" "1.8.0"\n'
                f'! _t3clean_vp_ok "{fake_bin}/vp" ""\n'
                f'_t3clean_ensure_vp "{src_catalog}"\n'
                f'test "$(command -v vp)" = "{cache_dir}/toolchains/vp-isolated-v1.8.0/bin/vp"\n'
                f'export PATH="{fake_bin}:$PATH"\n'
                f'_t3clean_ensure_vp "{src_none}"\n'
                f'test "$(command -v vp)" = "{cache_dir}/toolchains/vp-isolated/bin/vp"\n'
            )
            bash_run = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bash_run.returncode, 0, bash_run.stdout + bash_run.stderr)
            logged = npm_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(logged), 2)
            self.assertIn("vite-plus@1.8.0", logged[0])
            self.assertIn("vite-plus@^1.4.0", logged[1])

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
                # 1. Resolved fetch version propagates when --version is omitted
                rc = cli_mod.main(["build", "--source", str(src_dir), "--ref", "v0.1.12"])
                self.assertEqual(rc, 0)
                self.assertEqual(captured[-1].version, "0.1.12+notelemetry")

                # 2. Explicit --version overrides fetch_info["version"]
                rc = cli_mod.main(
                    [
                        "build",
                        "--source",
                        str(src_dir),
                        "--ref",
                        "v0.1.12",
                        "--version",
                        "0.1.12+custom",
                    ]
                )
                self.assertEqual(rc, 0)
                self.assertEqual(captured[-1].version, "0.1.12+custom")

                # 3. --skip-fetch leaves version=None so builder reads source metadata, and --jobs propagates
                fake_fetch.reset_mock()
                rc = cli_mod.main(["build", "--source", str(src_dir), "--skip-fetch", "--jobs", "3"])
                self.assertEqual(rc, 0)
                fake_fetch.assert_not_called()
                self.assertIsNone(captured[-1].version)
                self.assertEqual(captured[-1].jobs, 3)


if __name__ == "__main__":
    unittest.main()

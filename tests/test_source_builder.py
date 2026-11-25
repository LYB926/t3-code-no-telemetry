"""Unit tests for t3clean.source and t3clean.builder."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from t3clean import DEFAULT_NODE_VERSION
from t3clean.builder import (
    BuildOptions,
    VALID_TARGETS,
    build_target,
    ensure_isolated_toolchain,
    sanitized_build_env,
)
from t3clean.source import (
    EXACT_SERVICE_VERSION_RE,
    fetch_source,
    pick_latest_stable_tag,
    read_required_node_version,
    read_source_version,
    resolve_latest_stable_tag,
)
from t3clean.verifier import Finding, VerifyReport


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_local_git_repo(repo_dir: Path) -> dict[str, str]:
    """Create a real local git repository with stable, nightly, and main refs."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test User")
    _git(repo_dir, "config", "commit.gpgsign", "false")
    _git(repo_dir, "config", "tag.gpgsign", "false")

    (repo_dir / "package.json").write_text(
        json.dumps({"name": "monorepo", "engines": {"node": "26.13.1"}}),
        encoding="utf-8",
    )
    (repo_dir / "apps" / "server").mkdir(parents=True, exist_ok=True)
    (repo_dir / "apps" / "server" / "package.json").write_text(
        json.dumps({"name": "t3", "version": "0.1.11"}),
        encoding="utf-8",
    )
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "initial v0.1.11")
    sha_v11 = _git(repo_dir, "rev-parse", "HEAD")
    _git(repo_dir, "tag", "v0.1.11")

    (repo_dir / "README.txt").write_text("v0.1.12 commit\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "tag v0.1.12 before manifest bump")
    sha_v12 = _git(repo_dir, "rev-parse", "HEAD")
    _git(repo_dir, "tag", "v0.1.12")
    _git(repo_dir, "tag", "v0.1.13-nightly.20261125")
    _git(repo_dir, "tag", "v0.1.14-Preview.1")

    (repo_dir / "apps" / "server" / "package.json").write_text(
        json.dumps({"name": "t3", "version": "0.1.12"}),
        encoding="utf-8",
    )
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "post-release bump on main")
    sha_main = _git(repo_dir, "rev-parse", "HEAD")

    return {"v0.1.11": sha_v11, "v0.1.12": sha_v12, "main": sha_main}


class SourceTests(unittest.TestCase):
    def test_pick_latest_stable_tag_filters_and_sorts_semver(self) -> None:
        sample = "\n".join(
            [
                "1111111111111111111111111111111111111111\trefs/tags/v0.1.2",
                "2222222222222222222222222222222222222222\trefs/tags/v0.1.9",
                "3333333333333333333333333333333333333333\trefs/tags/v0.1.12",
                "4444444444444444444444444444444444444444\trefs/tags/v0.1.11",
                "5555555555555555555555555555555555555555\trefs/tags/v0.2.0-nightly.1",
                "6666666666666666666666666666666666666666\trefs/tags/v0.3.0-PREVIEW.2",
                "7777777777777777777777777777777777777777\trefs/tags/v0.4.0-rc.1",
                "8888888888888888888888888888888888888888\trefs/tags/v9.9",
                "9999999999999999999999999999999999999999\trefs/tags/v9.9.9^{}",
            ]
        )
        self.assertEqual(pick_latest_stable_tag(sample), "v0.1.12")

    def test_pick_latest_stable_tag_raises_when_no_stable_tags(self) -> None:
        sample = "aaaa\trefs/tags/v0.1.12-nightly.1\nbbbb\trefs/tags/v0.1.12-preview.1\n"
        with self.assertRaises(ValueError):
            pick_latest_stable_tag(sample)

    def test_resolve_latest_stable_tag_invokes_git_ls_remote(self) -> None:
        fake_stdout = "abcd\trefs/tags/v0.1.11\nef01\trefs/tags/v0.1.12\n"
        with mock.patch("t3clean.source.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_stdout, stderr=""
            )
            tag = resolve_latest_stable_tag("https://example.invalid/repo.git")
        self.assertEqual(tag, "v0.1.12")
        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(
            cmd,
            ["git", "ls-remote", "--tags", "--refs", "https://example.invalid/repo.git", "v[0-9]*"],
        )

    def test_read_source_version_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "apps" / "server").mkdir(parents=True)
            (src / "apps" / "desktop").mkdir(parents=True)
            (src / "apps" / "server" / "package.json").write_text(
                json.dumps({"version": "0.1.11"}), encoding="utf-8"
            )
            (src / "apps" / "desktop" / "package.json").write_text(
                json.dumps({"version": "0.1.10"}), encoding="utf-8"
            )

            v_tag = read_source_version(src, ref="v0.1.12")
            self.assertEqual(v_tag, "0.1.12+notelemetry")
            self.assertIsNotNone(EXACT_SERVICE_VERSION_RE.match(v_tag))

            v_main = read_source_version(src, ref="main")
            self.assertEqual(v_main, "0.1.11+notelemetry")

            # Fallback to apps/desktop/package.json when server package.json is absent.
            (src / "apps" / "server" / "package.json").unlink()
            self.assertEqual(read_source_version(src, ref="main"), "0.1.10+notelemetry")

            # Avoid duplicate `+` build metadata.
            v_meta = read_source_version(src, ref="v0.1.12+custom", suffix="+notelemetry")
            self.assertEqual(v_meta, "0.1.12+custom.notelemetry")
            self.assertIsNotNone(EXACT_SERVICE_VERSION_RE.match(v_meta))

    def test_read_required_node_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            self.assertEqual(read_required_node_version(src), DEFAULT_NODE_VERSION)

            (src / "package.json").write_text(
                json.dumps({"engines": {"node": "v26.14.0"}}), encoding="utf-8"
            )
            self.assertEqual(read_required_node_version(src), "26.14.0")

            (src / "package.json").write_text(
                json.dumps({"engines": {"node": ">=26.12.1"}}), encoding="utf-8"
            )
            self.assertEqual(read_required_node_version(src), "26.12.1")

            (src / "package.json").write_text(
                json.dumps({"engines": {"node": "^22.16 || >=24.10"}}), encoding="utf-8"
            )
            self.assertEqual(read_required_node_version(src), DEFAULT_NODE_VERSION)

    def test_fetch_source_clone_latest_stable_and_refetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            commits = _create_local_git_repo(upstream)
            dest = root / "checkout"

            info = fetch_source(dest, ref="latest-stable", repo_url=str(upstream))
            self.assertEqual(info["ref"], "latest-stable")
            self.assertEqual(info["resolved_ref"], "v0.1.12")
            self.assertEqual(info["commit"], commits["v0.1.12"])
            self.assertEqual(info["version"], "0.1.12+notelemetry")

            # Re-fetch existing checkout onto v0.1.11.
            info2 = fetch_source(dest, ref="v0.1.11", repo_url=str(upstream))
            self.assertEqual(info2["resolved_ref"], "v0.1.11")
            self.assertEqual(info2["commit"], commits["v0.1.11"])
            self.assertEqual(info2["version"], "0.1.11+notelemetry")
            self.assertEqual(read_source_version(dest), "0.1.11+notelemetry")

            # Re-fetch existing checkout from v0.1.11 onto a brand-new upstream tag (v0.1.13)
            # created AFTER initial clone, whose apps/server/package.json still reports 0.1.12.
            dest_upgrade = root / "checkout-upgrade"
            fetch_source(dest_upgrade, ref="v0.1.11", repo_url=f"file://{upstream}")
            self.assertEqual(read_source_version(dest_upgrade), "0.1.11+notelemetry")

            (upstream / "RELEASE_NOTE.txt").write_text("v0.1.13 release\n", encoding="utf-8")
            _git(upstream, "add", "RELEASE_NOTE.txt")
            _git(upstream, "commit", "-m", "tag v0.1.13 before manifest bump")
            sha_v13 = _git(upstream, "rev-parse", "HEAD")
            _git(upstream, "tag", "v0.1.13")
            self.assertEqual(_git(dest_upgrade, "tag", "-l", "v0.1.13"), "")

            info_upgrade = fetch_source(dest_upgrade, ref="v0.1.13", repo_url=f"file://{upstream}")
            self.assertEqual(info_upgrade["resolved_ref"], "v0.1.13")
            self.assertEqual(info_upgrade["commit"], sha_v13)
            self.assertEqual(info_upgrade["version"], "0.1.13+notelemetry")
            self.assertEqual(_git(dest_upgrade, "tag", "-l", "v0.1.13"), "v0.1.13")
            self.assertEqual(read_source_version(dest_upgrade), "0.1.13+notelemetry")

            # Clone directly from a raw commit SHA.
            dest_sha = root / "checkout-sha"
            info3 = fetch_source(dest_sha, ref=commits["main"], repo_url=str(upstream))
            self.assertEqual(info3["commit"], commits["main"])
            self.assertEqual(info3["version"], "0.1.12+notelemetry")


class BuilderTests(unittest.TestCase):
    def test_sanitized_build_env_scrubs_secrets_and_respects_cache_dir(self) -> None:
        dirty = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/tester",
            "T3CODE_CLERK_PUBLISHABLE_KEY": "pk_live_1234567890abcdef",
            "VITE_CLERK_JWT_TEMPLATE": "tpl",
            "EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY": "pk_test_1234567890abcdef",
            "T3CODE_RELAY_URL": "https://relay.example.com",
            "VITE_T3CODE_RELAY_URL": "https://relay.example.com",
            "VITE_RELAY_OTLP_TRACES_URL": "https://otlp.example.com",
            "T3CODE_MOBILE_OTLP_TRACES_TOKEN": "secret",
            "EXPO_PUBLIC_OTLP_TRACES_DATASET": "ds",
            "T3CODE_DESKTOP_UPDATE_REPOSITORY": "pingdotgg/t3code",
            "GITHUB_REPOSITORY": "pingdotgg/t3code",
            "CSC_LINK": "cert.p12",
            "CSC_KEY_PASSWORD": "pw",
            "T3CODE_TELEMETRY_ENABLED": "true",
        }

        no_cache = sanitized_build_env(dirty, cache_dir=None, sourcemap=False)
        for forbidden in (
            "T3CODE_CLERK_PUBLISHABLE_KEY",
            "VITE_CLERK_JWT_TEMPLATE",
            "EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY",
            "T3CODE_RELAY_URL",
            "VITE_T3CODE_RELAY_URL",
            "VITE_RELAY_OTLP_TRACES_URL",
            "T3CODE_MOBILE_OTLP_TRACES_TOKEN",
            "EXPO_PUBLIC_OTLP_TRACES_DATASET",
            "T3CODE_DESKTOP_UPDATE_REPOSITORY",
            "GITHUB_REPOSITORY",
            "CSC_LINK",
            "CSC_KEY_PASSWORD",
        ):
            self.assertNotIn(forbidden, no_cache)

        self.assertEqual(no_cache["T3CODE_TELEMETRY_ENABLED"], "false")
        self.assertEqual(no_cache["DO_NOT_TRACK"], "1")
        self.assertEqual(no_cache["T3CODE_TELEMETRY_CHANNEL"], "dev")
        self.assertEqual(no_cache["T3CODE_WEB_SOURCEMAP"], "false")
        for cache_key in (
            "ELECTRON_CACHE",
            "ELECTRON_BUILDER_CACHE",
            "CARGO_HOME",
            "PNPM_HOME",
            "npm_config_store_dir",
            "XDG_CACHE_HOME",
        ):
            self.assertNotIn(cache_key, no_cache)

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            with_cache = sanitized_build_env(dirty, cache_dir=cache_dir, sourcemap=True)
            self.assertEqual(with_cache["T3CODE_WEB_SOURCEMAP"], "true")
            self.assertEqual(with_cache["ELECTRON_CACHE"], str(cache_dir.resolve() / "electron"))
            self.assertEqual(
                with_cache["ELECTRON_BUILDER_CACHE"],
                str(cache_dir.resolve() / "electron-builder"),
            )
            self.assertEqual(with_cache["CARGO_HOME"], str(cache_dir.resolve() / "cargo"))
            self.assertEqual(with_cache["PNPM_HOME"], str(cache_dir.resolve() / "pnpm"))
            self.assertEqual(
                with_cache["npm_config_store_dir"],
                str(cache_dir.resolve() / "pnpm-store"),
            )
            self.assertEqual(with_cache["XDG_CACHE_HOME"], str(cache_dir.resolve() / "xdg"))

    def test_ensure_isolated_toolchain_downloads_verifies_sha256_and_installs_vp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            version = "26.13.1"
            stem = f"node-v{version}-linux-x64"
            tarball_name = f"{stem}.tar.xz"

            # Build a fake Node tarball in memory.
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:xz") as tar:
                for rel, body in (
                    (f"{stem}/bin/node", b"#!/bin/sh\necho v26.13.1\n"),
                    (f"{stem}/bin/npm", b"#!/bin/sh\nexit 0\n"),
                ):
                    info = tarfile.TarInfo(name=rel)
                    info.size = len(body)
                    info.mode = 0o755
                    tar.addfile(info, io.BytesIO(body))
            tarball_bytes = buf.getvalue()
            sha = hashlib.sha256(tarball_bytes).hexdigest()
            shasums = f"{sha}  {tarball_name}\n".encode("utf-8")

            def fake_download_bytes(url: str) -> bytes:
                self.assertTrue(url.endswith("/SHASUMS256.txt"))
                return shasums

            def fake_download_to_file(url: str, dest: Path) -> None:
                self.assertTrue(url.endswith(f"/{tarball_name}"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(tarball_bytes)

            def fake_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd[0].endswith("/npm"):
                    prefix = Path(cmd[cmd.index("--prefix") + 1])
                    vp = prefix / "bin" / "vp"
                    vp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    vp.chmod(0o755)
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
                if cmd[0].endswith("/node") and cmd[1] == "-v":
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout="v26.13.1\n", stderr=""
                    )
                raise AssertionError(f"Unexpected command: {cmd}")

            with (
                mock.patch("t3clean.builder.shutil.which", return_value=None),
                mock.patch("t3clean.builder._download_bytes", side_effect=fake_download_bytes),
                mock.patch("t3clean.builder._download_to_file", side_effect=fake_download_to_file),
                mock.patch("t3clean.builder.subprocess.run", side_effect=fake_subprocess_run),
            ):
                bin_dir = ensure_isolated_toolchain(cache_root, version, arch="x64")

            self.assertEqual(bin_dir, (cache_root / "toolchains" / stem / "bin").resolve())
            self.assertTrue((bin_dir / "node").is_file())
            self.assertTrue((bin_dir / "vp").is_file())

    def test_ensure_isolated_toolchain_rejects_bad_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            with (
                mock.patch("t3clean.builder.shutil.which", return_value=None),
                mock.patch(
                    "t3clean.builder._download_bytes",
                    return_value=b"0" * 64 + b"  node-v26.13.1-linux-x64.tar.xz\n",
                ),
                mock.patch(
                    "t3clean.builder._download_to_file",
                    side_effect=lambda _u, d: (d.parent.mkdir(parents=True, exist_ok=True), d.write_bytes(b"corrupt")),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "SHA-256 verification failed"):
                    ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64")

    def test_build_target_desktop_dir_and_system_electron_and_env_cleanup(self) -> None:
        self.assertEqual(
            VALID_TARGETS,
            ("desktop-dir", "desktop-deb", "desktop-appimage", "server-sea", "server-bundle"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            out = root / "out"
            (src / "apps" / "server").mkdir(parents=True)
            (src / "apps" / "server" / "package.json").write_text(
                json.dumps({"version": "0.1.12"}), encoding="utf-8"
            )
            (src / ".env").write_text("T3CODE_CLERK_PUBLISHABLE_KEY=pk_live_1234567890abcd\n", encoding="utf-8")
            (src / ".env.local").write_text("VITE_RELAY_OTLP_TRACES_URL=https://bad\n", encoding="utf-8")

            recorded_cmds: list[list[str]] = []

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                recorded_cmds.append(cmd)
                self.assertEqual(env["T3CODE_TELEMETRY_ENABLED"], "false")
                if "scripts/build-desktop-artifact.ts" in cmd:
                    unpacked = out / "linux-unpacked"
                    (unpacked / "resources").mkdir(parents=True, exist_ok=True)
                    (unpacked / "t3code").write_bytes(b"bundled-electron")
                    (unpacked / "resources" / "app.asar").write_bytes(b"clean-asar")

            with (
                mock.patch("t3clean.builder._run_cmd", side_effect=fake_run),
                mock.patch(
                    "t3clean.builder.verify_artifact",
                    return_value=VerifyReport(scanned_files=1, findings=[]),
                ) as verify_mock,
            ):
                artifact = build_target(
                    BuildOptions(
                        source_dir=src,
                        target="desktop-dir",
                        arch="x64",
                        version="0.1.12+notelemetry",
                        output_dir=out,
                        use_system_electron=True,
                        system_electron_bin="electron34",
                    )
                )

            self.assertFalse((src / ".env").exists())
            self.assertFalse((src / ".env.local").exists())
            self.assertEqual(artifact, out / "linux-system-electron")
            self.assertTrue((artifact / "resources" / "app.asar").is_file())
            self.assertFalse((artifact / "t3code").exists())
            self.assertEqual((artifact / ".use-system-electron").read_text(encoding="utf-8").strip(), "electron34")
            self.assertTrue((artifact / "SYSTEM_ELECTRON_WARNING.txt").is_file())
            self.assertEqual(recorded_cmds[0], ["vp", "install", "--frozen-lockfile"])
            self.assertEqual(recorded_cmds[1][:6], ["node", "scripts/build-desktop-artifact.ts", "--platform", "linux", "--target", "dir"])
            verify_mock.assert_called_once()

    def test_build_target_raises_when_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            out = root / "out"
            src.mkdir()

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                out.mkdir(parents=True, exist_ok=True)
                (out / "t3code-0.1.12.deb").write_bytes(b"fake-deb")

            failed_report = VerifyReport(
                scanned_files=1,
                findings=[Finding(path="app.asar", rule="posthog-host", detail="ingest.t3.codes")],
            )
            with (
                mock.patch("t3clean.builder._run_cmd", side_effect=fake_run),
                mock.patch("t3clean.builder.verify_artifact", return_value=failed_report),
            ):
                with self.assertRaisesRegex(RuntimeError, "Artifact verification failed"):
                    build_target(
                        BuildOptions(
                            source_dir=src,
                            target="desktop-deb",
                            version="0.1.12+notelemetry",
                            output_dir=out,
                            skip_install=True,
                        )
                    )

    def test_build_target_server_sea_checks_prerequisites_and_stages_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            out = root / "out"
            src.mkdir()

            # Missing dpkg-query / patchelf yields a clear actionable error.
            with mock.patch("t3clean.builder.shutil.which", side_effect=lambda name, **_: "/usr/bin/cargo" if name == "cargo" else None):
                with self.assertRaisesRegex(RuntimeError, "dpkg-query"):
                    build_target(
                        BuildOptions(
                            source_dir=src,
                            target="server-sea",
                            version="0.1.12+notelemetry",
                            output_dir=out,
                            skip_install=True,
                            skip_verify=True,
                        )
                    )

            cmds: list[list[str]] = []

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                cmds.append(cmd)
                if cmd[0] == "cargo":
                    mon = src / "native" / "resource-monitor" / "target" / "release" / "t3-resource-monitor"
                    mon.parent.mkdir(parents=True, exist_ok=True)
                    mon.write_bytes(b"elf-monitor")
                elif "scripts/build-cli-archive.ts" in cmd:
                    out.mkdir(parents=True, exist_ok=True)
                    archive = out / "t3-0.1.12+notelemetry-linux-x64.tar.gz"
                    with tarfile.open(archive, "w:gz") as tar:
                        info = tarfile.TarInfo("t3-0.1.12+notelemetry-linux-x64/t3")
                        info.size = 4
                        info.mode = 0o755
                        tar.addfile(info, io.BytesIO(b"bin!"))

            with (
                mock.patch("t3clean.builder.shutil.which", return_value="/usr/bin/tool"),
                mock.patch("t3clean.builder._run_cmd", side_effect=fake_run),
                mock.patch(
                    "t3clean.builder.verify_artifact",
                    return_value=VerifyReport(scanned_files=1, findings=[]),
                ),
            ):
                artifact = build_target(
                    BuildOptions(
                        source_dir=src,
                        target="server-sea",
                        arch="x64",
                        version="0.1.12+notelemetry",
                        output_dir=out,
                        skip_install=True,
                    )
                )

            self.assertEqual(artifact, out / "t3-0.1.12+notelemetry-linux-x64.tar.gz")
            staged_monitor = (
                src / "apps" / "server" / "dist" / "resource-monitor" / "linux-x64" / "t3-resource-monitor"
            )
            self.assertTrue(staged_monitor.is_file())
            self.assertEqual(len(cmds), 4)
            self.assertEqual(cmds[0], ["vp", "run", "--filter", "t3", "build"])
            self.assertEqual(cmds[2], ["node", "apps/server/scripts/cli.ts", "build-exe"])

    def test_build_target_server_bundle_packages_wrapper_dist_and_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            out = root / "out"
            fake_toolchain_bin = root / "toolchain" / "bin"
            fake_toolchain_bin.mkdir(parents=True)
            src.mkdir(parents=True)
            (fake_toolchain_bin / "node").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_toolchain_bin / "node").chmod(0o755)

            (src / "package.json").write_text(
                json.dumps({"engines": {"node": "26.13.1"}}), encoding="utf-8"
            )

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                if cmd == ["vp", "run", "--filter", "t3", "build"]:
                    dist = src / "apps" / "server" / "dist"
                    (dist / "client").mkdir(parents=True, exist_ok=True)
                    (dist / "bin.mjs").write_text("console.log('clean');\n", encoding="utf-8")
                    (dist / "bin.mjs.map").write_text("{}", encoding="utf-8")
                    (dist / "client" / "index.html").write_text("<html></html>\n", encoding="utf-8")

            with (
                mock.patch("t3clean.builder.ensure_isolated_toolchain", return_value=fake_toolchain_bin),
                mock.patch("t3clean.builder._run_cmd", side_effect=fake_run),
                mock.patch(
                    "t3clean.builder.verify_artifact",
                    return_value=VerifyReport(scanned_files=2, findings=[]),
                ),
            ):
                artifact = build_target(
                    BuildOptions(
                        source_dir=src,
                        target="server-bundle",
                        arch="x64",
                        version="0.1.12+notelemetry",
                        output_dir=out,
                        bootstrap_toolchain=True,
                        skip_install=True,
                    )
                )

            self.assertEqual(artifact, out / "t3-0.1.12+notelemetry-linux-x64-bundle.tar.gz")
            self.assertTrue(artifact.is_file())
            with tarfile.open(artifact, "r:gz") as tar:
                names = set(tar.getnames())
                stem = "t3-0.1.12+notelemetry-linux-x64-bundle"
                self.assertIn(f"{stem}/t3", names)
                self.assertIn(f"{stem}/dist/bin.mjs", names)
                self.assertIn(f"{stem}/dist/client/index.html", names)
                self.assertIn(f"{stem}/node/bin/node", names)
                self.assertNotIn(f"{stem}/dist/bin.mjs.map", names)
                wrapper_info = tar.getmember(f"{stem}/t3")
                self.assertTrue(bool(wrapper_info.mode & stat.S_IXUSR))
                wrapper_text = tar.extractfile(wrapper_info).read().decode("utf-8")
                self.assertIn('exec "$DIR/node/bin/node" "$DIR/dist/bin.mjs" "$@"', wrapper_text)


if __name__ == "__main__":
    unittest.main()

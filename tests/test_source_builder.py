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
    DEFAULT_VP_FALLBACK_SPEC,
    HostMemoryInfo,
    VALID_TARGETS,
    apply_concurrency_env,
    build_target,
    compute_adaptive_concurrency,
    detect_host_glibc_version,
    ensure_isolated_toolchain,
    format_low_memory_warning,
    read_host_memory_info,
    sanitized_build_env,
)
from t3clean.source import (
    EXACT_SERVICE_VERSION_RE,
    fetch_source,
    pick_latest_stable_tag,
    read_required_node_version,
    read_required_vp_version,
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

    def test_read_required_vp_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            self.assertIsNone(read_required_vp_version(src))

            # Upstream layout: catalog.vite-plus with overrides referencing "catalog:"
            (src / "pnpm-workspace.yaml").write_text(
                "packages:\n"
                "  - apps/*\n\n"
                "catalog:\n"
                "  vite: npm:@voidzero-dev/vite-plus-core@8.3.8001\n"
                "  vite-plus: 1.8.0\n\n"
                "overrides:\n"
                '  vite-plus: "catalog:"\n',
                encoding="utf-8",
            )
            self.assertEqual(read_required_vp_version(src), "1.8.0")

            # Quoted range + inline comment
            (src / "pnpm-workspace.yaml").write_text(
                "catalog:\n"
                '  "vite-plus": "^1.8.2" # pinned for build\n',
                encoding="utf-8",
            )
            self.assertEqual(read_required_vp_version(src), "1.8.2")

            # Prerelease tag
            (src / "pnpm-workspace.yaml").write_text(
                "catalog:\n"
                "  'vite-plus': '1.9.0-rc.1'\n",
                encoding="utf-8",
            )
            self.assertEqual(read_required_vp_version(src), "1.9.0-rc.1")

            # Missing from catalog (only under overrides) -> falls back to package.json
            (src / "pnpm-workspace.yaml").write_text(
                "catalog:\n"
                "  react: 19.3.0\n"
                "overrides:\n"
                "  vite-plus: 9.9.9\n",
                encoding="utf-8",
            )
            (src / "package.json").write_text(
                json.dumps({"devDependencies": {"vite-plus": "^1.7.0"}}),
                encoding="utf-8",
            )
            self.assertEqual(read_required_vp_version(src), "1.7.0")

            # package.json with "catalog:" specifier only -> None
            (src / "pnpm-workspace.yaml").unlink()
            (src / "package.json").write_text(
                json.dumps({"devDependencies": {"vite-plus": "catalog:"}}),
                encoding="utf-8",
            )
            self.assertIsNone(read_required_vp_version(src))

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

            npm_cmds: list[list[str]] = []

            def fake_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd[0].endswith("/npm"):
                    npm_cmds.append(cmd)
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
            self.assertEqual(npm_cmds[0][-1], DEFAULT_VP_FALLBACK_SPEC)

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

    def test_ensure_isolated_toolchain_reuses_host_node_and_installs_isolated_vp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            npm_calls: list[list[str]] = []

            def fake_which(name: str, **_kwargs: object) -> str | None:
                return {
                    "node": "/usr/bin/node",
                    "npm": "/usr/bin/npm",
                    "vp": "/usr/bin/vp",
                }.get(name)

            def fake_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd == ["/usr/bin/node", "-v"]:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="v26.13.1\n", stderr="")
                if cmd == ["/usr/bin/vp", "--version"]:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout="VITE+\nvp v1.3.0\n",
                        stderr="",
                    )
                if cmd[0] == "/usr/bin/npm":
                    npm_calls.append(cmd)
                    prefix = Path(cmd[cmd.index("--prefix") + 1])
                    vp = prefix / "bin" / "vp"
                    vp.parent.mkdir(parents=True, exist_ok=True)
                    vp.write_text("#!/bin/sh\necho 'vp v1.8.0'\n", encoding="utf-8")
                    vp.chmod(0o755)
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
                if cmd[0].endswith("/vp-isolated/bin/vp") and cmd[1] == "--version":
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="vp v1.8.0\n", stderr="")
                raise AssertionError(f"Unexpected command: {cmd}")

            with (
                mock.patch("t3clean.builder.shutil.which", side_effect=fake_which),
                mock.patch("t3clean.builder._download_bytes", side_effect=AssertionError("must not download Node")),
                mock.patch("t3clean.builder._download_to_file", side_effect=AssertionError("must not download Node")),
                mock.patch("t3clean.builder.subprocess.run", side_effect=fake_subprocess_run),
            ):
                bin_dir = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64")
                self.assertEqual(bin_dir, (cache_root / "toolchains" / "vp-isolated" / "bin").resolve())
                self.assertTrue((bin_dir / "vp").is_file())
                self.assertEqual(len(npm_calls), 1)
                self.assertEqual(npm_calls[0][-1], DEFAULT_VP_FALLBACK_SPEC)

                # Second call reuses cached vp >= 1.4.0 without running npm again.
                bin_dir2 = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64")
                self.assertEqual(bin_dir2, bin_dir)
                self.assertEqual(len(npm_calls), 1)

    def test_ensure_isolated_toolchain_pins_vp_version_and_blocks_major_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            npm_calls: list[list[str]] = []
            vp_versions: dict[str, str] = {"/usr/bin/vp": "vp v2.0.0\n"}

            def fake_which(name: str, **_kwargs: object) -> str | None:
                return {"node": "/usr/bin/node", "npm": "/usr/bin/npm", "vp": "/usr/bin/vp"}.get(name)

            def fake_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd == ["/usr/bin/node", "-v"]:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="v26.13.1\n", stderr="")
                if len(cmd) == 2 and cmd[1] == "--version":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout=vp_versions.get(cmd[0], ""),
                        stderr="",
                    )
                if cmd[0] == "/usr/bin/npm":
                    npm_calls.append(cmd)
                    prefix = Path(cmd[cmd.index("--prefix") + 1])
                    spec = cmd[-1]
                    vp = prefix / "bin" / "vp"
                    vp.parent.mkdir(parents=True, exist_ok=True)
                    vp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    vp.chmod(0o755)
                    installed_ver = spec.split("@", 1)[1].lstrip("^")
                    vp_versions[str(vp)] = f"vp v{installed_ver}\n"
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
                raise AssertionError(f"Unexpected command: {cmd}")

            with (
                mock.patch("t3clean.builder.shutil.which", side_effect=fake_which),
                mock.patch("t3clean.builder._download_bytes", side_effect=AssertionError("must not download Node")),
                mock.patch("t3clean.builder.subprocess.run", side_effect=fake_subprocess_run),
            ):
                # 1. Host vp 2.0.0 is rejected for pinned 1.8.0; installs exact vite-plus@1.8.0.
                bin_18 = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64", vp_version="1.8.0")
                self.assertEqual(bin_18, (cache_root / "toolchains" / "vp-isolated-v1.8.0" / "bin").resolve())
                self.assertEqual(npm_calls[-1][-1], "vite-plus@1.8.0")

                # 2. Second call reuses vp-isolated-v1.8.0 without running npm again.
                bin_18_again = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64", vp_version="1.8.0")
                self.assertEqual(bin_18_again, bin_18)
                self.assertEqual(len(npm_calls), 1)

                # 3. Unpinned fallback also rejects host vp 2.0.0 and installs vite-plus@^1.4.0.
                bin_fallback = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64", vp_version=None)
                self.assertEqual(bin_fallback, (cache_root / "toolchains" / "vp-isolated" / "bin").resolve())
                self.assertEqual(npm_calls[-1][-1], DEFAULT_VP_FALLBACK_SPEC)

                # 4. Future upstream bump to 2.1.0 rejects host 2.0.0 (< 2.1.0) and installs vite-plus@2.1.0.
                bin_21 = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64", vp_version="2.1.0")
                self.assertEqual(bin_21, (cache_root / "toolchains" / "vp-isolated-v2.1.0" / "bin").resolve())
                self.assertEqual(npm_calls[-1][-1], "vite-plus@2.1.0")

                # 5. Reuses compatible legacy toolchains/vp-isolated when versioned dir does not exist.
                legacy_cache = Path(tmp) / "legacy-cache"
                legacy_vp = legacy_cache / "toolchains" / "vp-isolated" / "bin" / "vp"
                legacy_vp.parent.mkdir(parents=True)
                legacy_vp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                legacy_vp.chmod(0o755)
                vp_versions[str(legacy_vp.resolve())] = "vp v1.8.0\n"
                before_npm = len(npm_calls)
                reused_legacy = ensure_isolated_toolchain(legacy_cache, "26.13.1", arch="x64", vp_version="1.8.0")
                self.assertEqual(reused_legacy, legacy_vp.parent.resolve())
                self.assertEqual(len(npm_calls), before_npm)

    def test_ensure_isolated_toolchain_reuses_host_node_and_modern_vp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            host_bin = Path(tmp) / "host-bin"
            host_bin.mkdir()
            fake_vp = host_bin / "vp"
            fake_vp.write_text("#!/bin/sh\n", encoding="utf-8")

            def fake_which(name: str, **_kwargs: object) -> str | None:
                return {"node": "/usr/bin/node", "npm": "/usr/bin/npm", "vp": str(fake_vp)}.get(name)

            def fake_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd == ["/usr/bin/node", "-v"]:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="v26.14.0\n", stderr="")
                if cmd == [str(fake_vp), "--version"]:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="vp v1.8.0\n", stderr="")
                raise AssertionError(f"Unexpected command: {cmd}")

            with (
                mock.patch("t3clean.builder.shutil.which", side_effect=fake_which),
                mock.patch("t3clean.builder._download_bytes", side_effect=AssertionError("must not download Node")),
                mock.patch("t3clean.builder.subprocess.run", side_effect=fake_subprocess_run),
            ):
                bin_dir = ensure_isolated_toolchain(cache_root, "26.13.1", arch="x64")

            self.assertEqual(bin_dir, host_bin.resolve())

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
            (src / "pnpm-workspace.yaml").write_text(
                "catalog:\n  vite-plus: 1.8.0\n", encoding="utf-8"
            )

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                if cmd == ["vp", "run", "--filter", "t3", "build"]:
                    dist = src / "apps" / "server" / "dist"
                    (dist / "client").mkdir(parents=True, exist_ok=True)
                    (dist / "bin.mjs").write_text("console.log('clean');\n", encoding="utf-8")
                    (dist / "bin.mjs.map").write_text("{}", encoding="utf-8")
                    (dist / "client" / "index.html").write_text("<html></html>\n", encoding="utf-8")

            with (
                mock.patch("t3clean.builder.ensure_isolated_toolchain", return_value=fake_toolchain_bin) as ensure_mock,
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

            ensure_mock.assert_called_once()
            self.assertEqual(ensure_mock.call_args.kwargs.get("vp_version"), "1.8.0")

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

    def test_read_host_memory_info_parses_meminfo_and_swaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meminfo = root / "meminfo"
            swaps = root / "swaps"
            meminfo.write_text(
                "MemTotal:       15518924 kB\n"
                "MemFree:          123456 kB\n"
                "MemAvailable:    4718592 kB\n",
                encoding="utf-8",
            )
            swaps.write_text(
                "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
                "/dev/zram0                              partition\t15518716\t0\t\t100\n",
                encoding="utf-8",
            )
            info = read_host_memory_info(meminfo_path=meminfo, swaps_path=swaps)
            self.assertEqual(info.total_mib, 15518924 // 1024)
            self.assertEqual(info.available_mib, 4718592 // 1024)
            self.assertTrue(info.has_zram_swap)
            self.assertFalse(info.has_disk_swap)

            # Adding a disk swapfile flips has_disk_swap to True.
            swaps.write_text(
                "Filename Type Size Used Priority\n"
                "/dev/zram0 partition 15518716 0 100\n"
                "/swapfile file 8388604 0 10\n",
                encoding="utf-8",
            )
            info2 = read_host_memory_info(meminfo_path=meminfo, swaps_path=swaps)
            self.assertTrue(info2.has_zram_swap)
            self.assertTrue(info2.has_disk_swap)

            # Missing /proc files return safe empty snapshot without raising.
            missing = read_host_memory_info(
                meminfo_path=root / "nonexistent-meminfo",
                swaps_path=root / "nonexistent-swaps",
            )
            self.assertEqual(missing, HostMemoryInfo())

    def test_adaptive_concurrency_and_low_memory_warning(self) -> None:
        # 1. 32 GiB workstation with ample available RAM -> no throttling, no warning.
        high_mem = HostMemoryInfo(total_mib=32 * 1024, available_mib=20 * 1024, has_disk_swap=True)
        self.assertEqual(compute_adaptive_concurrency(high_mem, cpu_count=16), {})
        self.assertIsNone(format_low_memory_warning(high_mem))

        # 2. 16 GiB laptop (14.8 GiB usable with iGPU) -> throttle to 4/4/2; preserves user overrides.
        mid_mem = HostMemoryInfo(total_mib=15155, available_mib=7 * 1024, has_zram_swap=True)
        self.assertEqual(
            compute_adaptive_concurrency(mid_mem, cpu_count=12),
            {
                "RAYON_NUM_THREADS": "4",
                "CARGO_BUILD_JOBS": "4",
                "npm_config_child_concurrency": "2",
            },
        )
        self.assertIsNone(format_low_memory_warning(mid_mem))

        env_auto = {"CARGO_BUILD_JOBS": "6"}
        applied = apply_concurrency_env(env_auto, mid_mem, explicit_jobs=None, cpu_count=12)
        self.assertEqual(applied["CARGO_BUILD_JOBS"], "6")
        self.assertEqual(applied["RAYON_NUM_THREADS"], "4")
        self.assertEqual(applied["npm_config_child_concurrency"], "2")

        # 3. Very low RAM (8 GiB or <4 GiB available) -> throttle to 2/2/1 + zram-only warning.
        low_mem = HostMemoryInfo(total_mib=15155, available_mib=3 * 1024 + 512, has_zram_swap=True)
        self.assertEqual(
            compute_adaptive_concurrency(low_mem, cpu_count=12),
            {
                "RAYON_NUM_THREADS": "2",
                "CARGO_BUILD_JOBS": "2",
                "npm_config_child_concurrency": "1",
            },
        )
        warn = format_low_memory_warning(
            low_mem,
            applied_limits={"RAYON_NUM_THREADS": "2", "CARGO_BUILD_JOBS": "2"},
        )
        self.assertIsNotNone(warn)
        assert warn is not None
        self.assertIn("zram-only swap detected", warn)
        self.assertIn("systemd-run", warn)
        self.assertIn("RAYON_NUM_THREADS=2", warn)

        # 4. Explicit jobs override ignores high-RAM bypass and overwrites existing env.
        env_explicit = {"CARGO_BUILD_JOBS": "12", "RAYON_NUM_THREADS": "12"}
        applied_explicit = apply_concurrency_env(env_explicit, high_mem, explicit_jobs=3, cpu_count=16)
        self.assertEqual(
            applied_explicit,
            {
                "RAYON_NUM_THREADS": "3",
                "CARGO_BUILD_JOBS": "3",
                "npm_config_child_concurrency": "3",
            },
        )
        with self.assertRaises(ValueError):
            compute_adaptive_concurrency(high_mem, explicit_jobs=0)

    def test_build_target_applies_adaptive_concurrency_and_emits_low_memory_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            out = root / "out"
            src.mkdir()
            captured_envs: list[dict[str, str]] = []

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                captured_envs.append(dict(env))
                unpacked = out / "linux-unpacked"
                (unpacked / "resources").mkdir(parents=True, exist_ok=True)
                (unpacked / "t3code").write_bytes(b"bin")
                (unpacked / "resources" / "app.asar").write_bytes(b"clean")

            tight_mem = HostMemoryInfo(total_mib=15155, available_mib=4 * 1024 + 512, has_zram_swap=True)
            stderr_buf = io.StringIO()
            clean_base_env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "RAYON_NUM_THREADS",
                    "CARGO_BUILD_JOBS",
                    "npm_config_child_concurrency",
                    "T3CLEAN_JOBS",
                    "T3CODE_GLIBC_BUILD_CEILING",
                )
            }
            with (
                mock.patch.dict(os.environ, clean_base_env, clear=True),
                mock.patch("t3clean.builder.read_host_memory_info", return_value=tight_mem),
                mock.patch("t3clean.builder.detect_host_glibc_version", return_value="2.44"),
                mock.patch("t3clean.builder.os.cpu_count", return_value=12),
                mock.patch("t3clean.builder._run_cmd", side_effect=fake_run),
                mock.patch("sys.stderr", stderr_buf),
            ):
                build_target(
                    BuildOptions(
                        source_dir=src,
                        target="desktop-dir",
                        version="0.1.12+notelemetry",
                        output_dir=out,
                        skip_install=True,
                        skip_verify=True,
                    )
                )

            self.assertEqual(len(captured_envs), 1)
            self.assertEqual(captured_envs[0]["RAYON_NUM_THREADS"], "4")
            self.assertEqual(captured_envs[0]["CARGO_BUILD_JOBS"], "4")
            self.assertEqual(captured_envs[0]["npm_config_child_concurrency"], "2")
            self.assertEqual(captured_envs[0]["T3CODE_GLIBC_BUILD_CEILING"], "2.44")
            self.assertIn(str(src.resolve().parent), captured_envs[0]["GIT_CEILING_DIRECTORIES"].split(os.pathsep))
            stderr_text = stderr_buf.getvalue()
            self.assertIn("Auto-throttled build concurrency", stderr_text)
            self.assertIn("WARNING: Low available memory", stderr_text)
            self.assertIn("zram-only swap detected", stderr_text)

    def test_detect_host_glibc_version_and_user_override(self) -> None:
        with mock.patch("t3clean.builder.os.confstr", return_value="glibc 2.44"):
            self.assertEqual(detect_host_glibc_version(), "2.44")
        with mock.patch("t3clean.builder.os.confstr", return_value="glibc 2.39"):
            self.assertEqual(detect_host_glibc_version(), "2.39")
        with mock.patch("t3clean.builder.os.confstr", side_effect=OSError("unsupported")):
            self.assertIsNone(detect_host_glibc_version())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            out = root / "out"
            src.mkdir()
            seen_ceiling: list[str] = []

            def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
                seen_ceiling.append(env.get("T3CODE_GLIBC_BUILD_CEILING", ""))
                unpacked = out / "linux-unpacked"
                (unpacked / "resources").mkdir(parents=True, exist_ok=True)
                (unpacked / "t3code").write_bytes(b"bin")

            with (
                mock.patch.dict(os.environ, {"T3CODE_GLIBC_BUILD_CEILING": "2.39"}),
                mock.patch("t3clean.builder.detect_host_glibc_version", return_value="2.44"),
                mock.patch("t3clean.builder._run_cmd", side_effect=fake_run),
            ):
                build_target(
                    BuildOptions(
                        source_dir=src,
                        target="desktop-dir",
                        version="0.1.12+notelemetry",
                        output_dir=out,
                        skip_install=True,
                        skip_verify=True,
                    )
                )
            self.assertEqual(seen_ceiling, ["2.39"])


if __name__ == "__main__":
    unittest.main()

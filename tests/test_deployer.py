"""Unit tests for t3clean.deployer."""

from __future__ import annotations

import io
import json
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from t3clean.deployer import (
    DeployOptions,
    deploy_server,
    list_backups,
    rollback_server,
    safe_extract_tar,
)
from t3clean.verifier import Finding, VerifyReport


def _build_runtime_archive(
    dest_archive: Path,
    version: str = "0.1.12+notelemetry",
    *,
    binary_content: bytes = b"#!/bin/sh\necho new-runtime\n",
    client_js: bytes = b"console.log('clean');\n",
) -> Path:
    stem = f"t3-{version}-linux-x64"
    dest_archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_archive, "w:gz") as tar:
        files = {
            f"{stem}/t3": (binary_content, 0o755),
            f"{stem}/client/index.html": (b"<!doctype html><html></html>\n", 0o644),
            f"{stem}/client/app.js": (client_js, 0o644),
            f"{stem}/lib/libatomic.so.1": (b"fake-so", 0o644),
            f"{stem}/resource-monitor/linux-x64/t3-resource-monitor": (b"mon", 0o755),
        }
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return dest_archive


def _seed_existing_host(root: Path) -> tuple[Path, Path, Path]:
    """Create a realistic Ubuntu aiseon ~/.t3, ~/.local/bin, and systemd unit layout."""
    t3_home = root / ".t3"
    bin_dir = root / ".local" / "bin"
    unit_dir = root / ".config" / "systemd" / "user"

    prev_ver = "0.1.11"
    prev_dir = t3_home / "runtime" / "versions" / prev_ver
    (prev_dir / "client").mkdir(parents=True)
    (prev_dir / "t3").write_text("#!/bin/sh\necho old-runtime\n", encoding="utf-8")
    (prev_dir / "t3").chmod(0o755)
    (prev_dir / ".install-complete").write_text("ok\n", encoding="utf-8")

    (t3_home / "runtime" / "service-state.json").write_text(
        json.dumps({"protocol": 4, "activeVersion": prev_ver}, indent=2) + "\n",
        encoding="utf-8",
    )

    unit_dir.mkdir(parents=True)
    (unit_dir / "t3code.service").write_text(
        (
            "[Unit]\n"
            "Description=T3 Code Server\n"
            "\n"
            "[Service]\n"
            f"ExecStart={prev_dir / 't3'} __service-launcher\n"
            f"Environment=T3CODE_HOME={t3_home}\n"
            "Environment=T3_BOOT_SERVICE_UNIT=t3code.service\n"
            "Restart=always\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ),
        encoding="utf-8",
    )
    dropin_dir = unit_dir / "t3code.service.d"
    dropin_dir.mkdir(parents=True)
    (dropin_dir / "proxy.conf").write_text(
        "[Service]\nEnvironment=HTTP_PROXY=http://127.0.0.1:7899\nEnvironment=HTTPS_PROXY=http://127.0.0.1:7899\n",
        encoding="utf-8",
    )
    (dropin_dir / "telemetry.conf").write_text(
        "[Service]\nEnvironment=T3CODE_TELEMETRY_ENABLED=false\n",
        encoding="utf-8",
    )

    bin_dir.mkdir(parents=True)
    (bin_dir / "t3").write_text(f"#!/bin/sh\nexec '{prev_dir / 't3'}' \"$@\"\n", encoding="utf-8")
    (bin_dir / "t3").chmod(0o755)

    (t3_home / "service.env").write_text(
        "T3CODE_PORT=3773\nT3CODE_TELEMETRY_ENABLED=true\n",
        encoding="utf-8",
    )

    userdata = t3_home / "userdata"
    userdata.mkdir(parents=True)
    (userdata / "anonymous-id").write_text("anon-uuid-1234\n", encoding="utf-8")

    # Populate a WAL-mode SQLite database to test online sqlite3 backup.
    db_path = userdata / "statev2.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL);")
        conn.execute("INSERT INTO threads (id, title) VALUES ('t1', 'original-title');")
        conn.commit()
    finally:
        conn.close()

    return t3_home, bin_dir, unit_dir


class DeployerTests(unittest.TestCase):
    def test_safe_extract_tar_blocks_path_traversal_and_escaping_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. Parent traversal (../escape.txt)
            bad_parent = root / "bad-parent.tar.gz"
            with tarfile.open(bad_parent, "w:gz") as tar:
                info = tarfile.TarInfo("../escape.txt")
                info.size = 3
                tar.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaises(ValueError):
                safe_extract_tar(bad_parent, root / "out1")

            # 2. Absolute path (/tmp/pwn)
            bad_abs = root / "bad-abs.tar.gz"
            with tarfile.open(bad_abs, "w:gz") as tar:
                info = tarfile.TarInfo("/tmp/pwn")
                info.size = 3
                tar.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaises(ValueError):
                safe_extract_tar(bad_abs, root / "out2")

            # 3. Symlink escaping extraction root
            bad_sym = root / "bad-sym.tar.gz"
            with tarfile.open(bad_sym, "w:gz") as tar:
                info = tarfile.TarInfo("pkg/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                tar.addfile(info)
            with self.assertRaises(ValueError):
                safe_extract_tar(bad_sym, root / "out3")

            # 4. Hardlink escaping extraction root
            bad_hard = root / "bad-hard.tar.gz"
            with tarfile.open(bad_hard, "w:gz") as tar:
                info = tarfile.TarInfo("pkg/hard")
                info.type = tarfile.LNKTYPE
                info.linkname = "../outside"
                tar.addfile(info)
            with self.assertRaises(ValueError):
                safe_extract_tar(bad_hard, root / "out4")

    def test_deploy_server_full_lifecycle_backup_outside_versions_and_sqlite_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")

            systemctl_calls: list[list[str]] = []

            def fake_systemctl(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                systemctl_calls.append(cmd)
                stdout = "active\n" if "is-active" in cmd else ""
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

            with (
                mock.patch("t3clean.deployer.subprocess.run", side_effect=fake_systemctl),
                mock.patch(
                    "t3clean.deployer.verify_artifact",
                    return_value=VerifyReport(scanned_files=3, findings=[]),
                ),
            ):
                result = deploy_server(
                    DeployOptions(
                        archive_path=archive,
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                    )
                )

            self.assertEqual(result["status"], "deployed")
            self.assertEqual(result["version"], "0.1.12+notelemetry")
            self.assertEqual(result["previous_version"], "0.1.11")
            self.assertEqual(result["systemctl_status"], "active")
            self.assertEqual(result["anonymous_id_removed"], "true")

            # CRITICAL: Backup must live outside runtime/versions/ and be named by version.
            backup_dir = Path(result["backup_dir"])
            versions_dir = t3_home / "runtime" / "versions"
            self.assertTrue(backup_dir.is_dir())
            self.assertEqual(backup_dir.name, "0.1.11")
            self.assertEqual(backup_dir.parent, t3_home / "runtime" / "no-telemetry-backups")
            self.assertNotIn(versions_dir, backup_dir.parents)

            # Verify systemd drop-ins (e.g. proxy.conf) preserved live AND backed up.
            self.assertIn(
                "HTTP_PROXY=http://127.0.0.1:7899",
                (unit_dir / "t3code.service.d" / "proxy.conf").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "HTTP_PROXY=http://127.0.0.1:7899",
                (backup_dir / "t3code.service.d" / "proxy.conf").read_text(encoding="utf-8"),
            )

            # Verify SQLite online backup integrity.
            backup_db = backup_dir / "statev2.sqlite"
            self.assertTrue(backup_db.is_file())
            conn = sqlite3.connect(str(backup_db))
            try:
                rows = conn.execute("SELECT id, title FROM threads").fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [("t1", "original-title")])

            # Verify new runtime installed with .install-complete and 0755 binary.
            new_runtime = versions_dir / "0.1.12+notelemetry"
            self.assertTrue((new_runtime / ".install-complete").is_file())
            self.assertTrue(bool((new_runtime / "t3").stat().st_mode & stat.S_IXUSR))

            # Verify service-state.json updated.
            state = json.loads((t3_home / "runtime" / "service-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state, {"protocol": 4, "activeVersion": "0.1.12+notelemetry"})

            # Verify systemd unit ExecStart updated while keeping Environment lines.
            unit_text = (unit_dir / "t3code.service").read_text(encoding="utf-8")
            self.assertIn(f"ExecStart={new_runtime / 't3'} __service-launcher", unit_text)
            self.assertIn(f"Environment=T3CODE_HOME={t3_home}", unit_text)
            self.assertIn("Environment=T3_BOOT_SERVICE_UNIT=t3code.service", unit_text)

            # Verify ~/.local/bin/t3 wrapper.
            bin_wrapper = bin_dir / "t3"
            self.assertEqual(
                bin_wrapper.read_text(encoding="utf-8"),
                f"#!/bin/sh\nexec '{new_runtime / 't3'}' \"$@\"\n",
            )
            self.assertTrue(bool(bin_wrapper.stat().st_mode & stat.S_IXUSR))

            # Verify service.env preserves existing key and disables telemetry.
            env_text = (t3_home / "service.env").read_text(encoding="utf-8")
            self.assertIn("T3CODE_PORT=3773", env_text)
            self.assertIn("T3CODE_TELEMETRY_ENABLED=false", env_text)
            self.assertNotIn("T3CODE_TELEMETRY_ENABLED=true", env_text)
            self.assertIn("DO_NOT_TRACK=1", env_text)

            # Verify anonymous-id removed.
            self.assertFalse((t3_home / "userdata" / "anonymous-id").exists())

            # Verify systemctl call sequence.
            self.assertEqual(
                systemctl_calls,
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "restart", "t3code.service"],
                    ["systemctl", "--user", "is-active", "t3code.service"],
                ],
            )

    def test_deploy_server_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")

            with (
                mock.patch("t3clean.deployer.subprocess.run") as run_mock,
                mock.patch(
                    "t3clean.deployer.verify_artifact",
                    return_value=VerifyReport(scanned_files=2, findings=[]),
                ),
            ):
                result = deploy_server(
                    DeployOptions(
                        archive_path=archive,
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        dry_run=True,
                    )
                )

            self.assertEqual(result["status"], "dry-run")
            self.assertEqual(result["version"], "0.1.12+notelemetry")
            run_mock.assert_not_called()
            self.assertFalse((t3_home / "runtime" / "no-telemetry-backups").exists())
            self.assertFalse((t3_home / "runtime" / "versions" / "0.1.12+notelemetry").exists())
            self.assertTrue((t3_home / "userdata" / "anonymous-id").exists())

    def test_deploy_server_aborts_before_mutation_on_verify_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")

            bad_report = VerifyReport(
                scanned_files=1,
                findings=[Finding(path="client/app.js", rule="posthog-host", detail="ingest.t3.codes")],
            )
            with mock.patch("t3clean.deployer.verify_artifact", return_value=bad_report):
                with self.assertRaisesRegex(RuntimeError, "failed verification"):
                    deploy_server(
                        DeployOptions(
                            archive_path=archive,
                            t3_home=t3_home,
                            bin_dir=bin_dir,
                            unit_dir=unit_dir,
                            skip_systemctl=True,
                        )
                    )

            self.assertFalse((t3_home / "runtime" / "no-telemetry-backups").exists())
            state = json.loads((t3_home / "runtime" / "service-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["activeVersion"], "0.1.11")

    def test_deploy_server_auto_rollbacks_when_restart_or_health_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")

            restart_attempt = {"count": 0}

            def flaky_systemctl(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if "restart" in cmd:
                    restart_attempt["count"] += 1
                    if restart_attempt["count"] == 1:
                        raise subprocess.CalledProcessError(
                            returncode=1,
                            cmd=cmd,
                            stderr="Job for t3code.service failed",
                        )
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
                if "is-active" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="active\n", stderr="")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            with (
                mock.patch("t3clean.deployer.subprocess.run", side_effect=flaky_systemctl),
                mock.patch(
                    "t3clean.deployer.verify_artifact",
                    return_value=VerifyReport(scanned_files=2, findings=[]),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "automatic rollback to 0.1.11 succeeded"):
                    deploy_server(
                        DeployOptions(
                            archive_path=archive,
                            t3_home=t3_home,
                            bin_dir=bin_dir,
                            unit_dir=unit_dir,
                        )
                    )

            # Confirm everything was restored to 0.1.11!
            state = json.loads((t3_home / "runtime" / "service-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["activeVersion"], "0.1.11")
            unit_text = (unit_dir / "t3code.service").read_text(encoding="utf-8")
            self.assertIn("versions/0.1.11/t3 __service-launcher", unit_text)
            self.assertIn("versions/0.1.11/t3", (bin_dir / "t3").read_text(encoding="utf-8"))
            self.assertIn("T3CODE_TELEMETRY_ENABLED=true", (t3_home / "service.env").read_text(encoding="utf-8"))
            self.assertEqual(restart_attempt["count"], 2)

    def test_list_backups_and_manual_rollback_with_optional_db_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")

            with mock.patch(
                "t3clean.deployer.verify_artifact",
                return_value=VerifyReport(scanned_files=2, findings=[]),
            ):
                deploy_res = deploy_server(
                    DeployOptions(
                        archive_path=archive,
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        skip_systemctl=True,
                    )
                )

            backups = list_backups(t3_home)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0]["backup_id"], deploy_res["backup_id"])
            self.assertEqual(backups[0]["previous_version"], "0.1.11")
            self.assertEqual(backups[0]["target_version"], "0.1.12+notelemetry")
            self.assertEqual(backups[0]["has_db_backup"], "true")

            # Mutate the live DB after deploy so we can verify restore_db=False vs True.
            db_path = t3_home / "userdata" / "statev2.sqlite"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("INSERT INTO threads (id, title) VALUES ('t2', 'post-deploy-title');")
                conn.commit()
            finally:
                conn.close()

            # Rollback without restore_db keeps the newer DB row.
            rb1 = rollback_server(
                t3_home,
                bin_dir,
                unit_dir=unit_dir,
                restore_db=False,
                skip_systemctl=True,
            )
            self.assertEqual(rb1["restored_version"], "0.1.11")
            self.assertEqual(rb1["db_restored"], "false")
            conn = sqlite3.connect(str(db_path))
            try:
                rows_after_rb1 = conn.execute("SELECT id FROM threads ORDER BY id").fetchall()
            finally:
                conn.close()
            self.assertEqual(rows_after_rb1, [("t1",), ("t2",)])

            # Rollback WITH restore_db restores the exact pre-deploy snapshot.
            rb2 = rollback_server(
                t3_home,
                bin_dir,
                unit_dir=unit_dir,
                backup_id=deploy_res["backup_id"],
                restore_db=True,
                skip_systemctl=True,
            )
            self.assertEqual(rb2["db_restored"], "true")
            conn = sqlite3.connect(str(db_path))
            try:
                rows_after_rb2 = conn.execute("SELECT id, title FROM threads ORDER BY id").fetchall()
            finally:
                conn.close()
            self.assertEqual(rows_after_rb2, [("t1", "original-title")])

    def test_deploy_server_creates_fresh_systemd_unit_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home = root / ".t3"
            bin_dir = root / ".local" / "bin"
            unit_dir = root / ".config" / "systemd" / "user"
            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")

            with mock.patch(
                "t3clean.deployer.verify_artifact",
                return_value=VerifyReport(scanned_files=2, findings=[]),
            ):
                result = deploy_server(
                    DeployOptions(
                        archive_path=archive,
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        skip_systemctl=True,
                    )
                )

            unit_text = (unit_dir / "t3code.service").read_text(encoding="utf-8")
            self.assertIn("[Unit]", unit_text)
            self.assertIn("[Service]", unit_text)
            self.assertIn(f"ExecStart={result['install_dir']}/t3 __service-launcher", unit_text)
            self.assertIn(f"Environment=T3CODE_HOME={t3_home}", unit_text)
            self.assertIn("Environment=T3CODE_TELEMETRY_ENABLED=false", unit_text)

    def test_deploy_server_real_verifier_catches_leak_in_sea_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            dirty_archive = _build_runtime_archive(
                root / "t3-0.1.12+notelemetry-linux-x64.tar.gz",
                binary_content=b"\x7fELF...https://ingest.t3.codes/e/...",
            )
            with self.assertRaisesRegex(RuntimeError, "failed verification"):
                deploy_server(
                    DeployOptions(
                        archive_path=dirty_archive,
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        skip_systemctl=True,
                    )
                )
            self.assertFalse((t3_home / "runtime" / "no-telemetry-backups").exists())

    def test_rollback_restores_non_utf8_binary_bin_wrapper_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)
            raw_binary = b"\x7fELF\x02\x01\x01\x00\xff\xfe\xfd\x80fake-elf-launcher"
            (bin_dir / "t3").write_bytes(raw_binary)
            (bin_dir / "t3").chmod(0o755)

            archive = _build_runtime_archive(root / "t3-0.1.12+notelemetry-linux-x64.tar.gz")
            deploy_server(
                DeployOptions(
                    archive_path=archive,
                    t3_home=t3_home,
                    bin_dir=bin_dir,
                    unit_dir=unit_dir,
                    skip_systemctl=True,
                )
            )
            self.assertNotEqual((bin_dir / "t3").read_bytes(), raw_binary)

            rollback_server(
                t3_home,
                bin_dir,
                unit_dir=unit_dir,
                skip_systemctl=True,
            )
            self.assertEqual((bin_dir / "t3").read_bytes(), raw_binary)
            self.assertTrue(bool((bin_dir / "t3").stat().st_mode & stat.S_IXUSR))

    def test_deploy_server_prunes_oldest_backups_and_restores_dropins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t3_home, bin_dir, unit_dir = _seed_existing_host(root)

            with mock.patch(
                "t3clean.deployer.verify_artifact",
                return_value=VerifyReport(scanned_files=2, findings=[]),
            ):
                with self.assertRaises(ValueError):
                    deploy_server(
                        DeployOptions(
                            archive_path=_build_runtime_archive(root / "a0.tar.gz", "0.1.12+notelemetry"),
                            t3_home=t3_home,
                            bin_dir=bin_dir,
                            unit_dir=unit_dir,
                            skip_systemctl=True,
                            max_backups=0,
                        )
                    )

                r1 = deploy_server(
                    DeployOptions(
                        archive_path=_build_runtime_archive(root / "a1.tar.gz", "0.1.12+notelemetry"),
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        skip_systemctl=True,
                        max_backups=2,
                    )
                )
                self.assertEqual(r1["backup_id"], "0.1.11")
                self.assertEqual(r1["pruned_backups"], "")

                r2 = deploy_server(
                    DeployOptions(
                        archive_path=_build_runtime_archive(root / "a2.tar.gz", "0.1.13+notelemetry"),
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        skip_systemctl=True,
                        max_backups=2,
                    )
                )
                self.assertEqual(r2["backup_id"], "0.1.12+notelemetry")
                self.assertEqual(r2["pruned_backups"], "")
                self.assertEqual([b["backup_id"] for b in list_backups(t3_home)], ["0.1.12+notelemetry", "0.1.11"])

                # Third deploy exceeds max_backups=2 -> oldest (0.1.11) is pruned.
                r3 = deploy_server(
                    DeployOptions(
                        archive_path=_build_runtime_archive(root / "a3.tar.gz", "0.1.14+notelemetry"),
                        t3_home=t3_home,
                        bin_dir=bin_dir,
                        unit_dir=unit_dir,
                        skip_systemctl=True,
                        max_backups=2,
                    )
                )
                self.assertEqual(r3["backup_id"], "0.1.13+notelemetry")
                self.assertEqual(r3["pruned_backups"], "0.1.11")
                self.assertEqual(
                    [b["backup_id"] for b in list_backups(t3_home)],
                    ["0.1.13+notelemetry", "0.1.12+notelemetry"],
                )
                self.assertFalse((t3_home / "runtime" / "no-telemetry-backups" / "0.1.11").exists())

                # Simulate drop-in deletion and confirm rollback restores proxy.conf.
                (unit_dir / "t3code.service.d" / "proxy.conf").unlink()
                rollback_server(
                    t3_home,
                    bin_dir,
                    unit_dir=unit_dir,
                    skip_systemctl=True,
                )
                self.assertIn(
                    "HTTP_PROXY=http://127.0.0.1:7899",
                    (unit_dir / "t3code.service.d" / "proxy.conf").read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()

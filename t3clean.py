#!/usr/bin/env python3
"""CLI entrypoint for t3-code-no-telemetry (t3clean).

Pure Python 3.10+ standard-library tool for fetching, de-telemetrizing,
verifying, building, deploying, rolling back, and exporting AUR packages for
T3 Code without modifying upstream defaults unsafely.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from t3clean import DEFAULT_REF, DEFAULT_REPO_URL  # noqa: E402
from t3clean.aur import export_aur_package  # noqa: E402
from t3clean.builder import BuildOptions, VALID_TARGETS, build_target  # noqa: E402
from t3clean.deployer import (  # noqa: E402
    DeployOptions,
    deploy_server,
    list_backups,
    rollback_server,
)
from t3clean.patcher import (  # noqa: E402
    PatchAnchorNotFoundError,
    PatchOptions,
    patch_source_tree,
)
from t3clean.source import fetch_source, resolve_latest_github_release  # noqa: E402
from t3clean.verifier import VerifyOptions, verify_artifact  # noqa: E402


def _add_json_flag(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    if root:
        parser.add_argument(
            "--json",
            action="store_true",
            default=False,
            help="Emit machine-readable JSON output.",
        )
    else:
        parser.add_argument(
            "--json",
            dest="json_sub",
            action="store_true",
            default=False,
            help="Emit machine-readable JSON output.",
        )


def _wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False) or getattr(args, "json_sub", False))


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _emit_error(message: str, *, as_json: bool) -> int:
    if as_json:
        _emit_json({"ok": False, "error": message})
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _cmd_fetch(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    dest = Path(args.dest).expanduser().resolve()
    result = fetch_source(dest=dest, ref=args.ref, repo_url=args.repo_url)
    if as_json:
        _emit_json({"ok": True, "dest": str(dest), **result})
    else:
        print(f"Fetched {result.get('resolved_ref', args.ref)} -> {dest}")
        print(f"  commit : {result.get('commit', 'unknown')}")
        print(f"  version: {result.get('version', 'unknown')}")
    return 0


def _cmd_patch(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    source_dir = Path(args.source).expanduser().resolve()
    options = PatchOptions(
        keep_updater=bool(args.keep_updater),
        keep_cloud=bool(args.keep_cloud),
        dry_run=bool(args.dry_run),
    )
    results = patch_source_tree(source_dir, options)
    if as_json:
        _emit_json(
            {
                "ok": True,
                "source": str(source_dir),
                "dry_run": options.dry_run,
                "results": [asdict(item) for item in results],
            }
        )
    else:
        mode = " (dry-run)" if options.dry_run else ""
        print(f"Patched source tree{mode}: {source_dir}")
        for item in results:
            if item.already_patched and not item.changed:
                status = "ALREADY"
            elif item.changed and options.dry_run:
                status = "DRY-RUN"
            elif item.changed:
                status = "PATCHED"
            else:
                status = "SKIPPED"
            print(f"  [{status:7s}] {item.rule_id}: {item.relative_path} — {item.summary}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    target_path = Path(args.artifact).expanduser().resolve()
    options = VerifyOptions(
        keep_updater=bool(args.keep_updater),
        strict_offline=bool(args.strict_offline),
    )
    report = verify_artifact(target_path, options)
    if as_json:
        _emit_json(
            {
                "ok": report.ok,
                "artifact": str(target_path),
                "scanned_files": report.scanned_files,
                "findings": [asdict(finding) for finding in report.findings],
            }
        )
    else:
        if report.ok:
            print(
                f"OK: verified {report.scanned_files} file(s) under {target_path} with 0 findings."
            )
        else:
            print(
                f"FAILED: found {len(report.findings)} forbidden telemetry/cloud match(es) "
                f"across {report.scanned_files} scanned file(s) under {target_path}:",
                file=sys.stderr,
            )
            for finding in report.findings:
                print(
                    f"  - [{finding.rule}] {finding.path}: {finding.detail}",
                    file=sys.stderr,
                )
    return 0 if report.ok else 1


def _cmd_build(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    source_dir = (
        Path(args.source).expanduser().resolve()
        if args.source
        else (Path.cwd() / ".t3clean-work" / "t3code").resolve()
    )

    fetch_info: dict[str, str] | None = None
    if not args.skip_fetch:
        fetch_info = fetch_source(dest=source_dir, ref=args.ref, repo_url=args.repo_url)

    patch_results = None
    if not args.skip_patch:
        patch_results = patch_source_tree(
            source_dir,
            PatchOptions(
                keep_updater=bool(args.keep_updater),
                keep_cloud=bool(args.keep_cloud),
                dry_run=False,
            ),
        )

    resolved_version = args.version or (fetch_info.get("version") if fetch_info else None)

    build_opts = BuildOptions(
        source_dir=source_dir,
        target=args.target,
        arch=args.arch,
        version=resolved_version,
        output_dir=Path(args.output_dir).expanduser().resolve() if args.output_dir else None,
        cache_dir=Path(args.cache_dir).expanduser().resolve() if args.cache_dir else None,
        jobs=args.jobs,
        bootstrap_toolchain=bool(args.bootstrap_toolchain),
        use_system_electron=bool(args.use_system_electron),
        system_electron_bin=args.system_electron_bin,
        sourcemap=bool(args.sourcemap),
        skip_install=bool(args.skip_install),
        skip_verify=bool(args.skip_verify),
        keep_updater=bool(args.keep_updater),
        keep_cloud=bool(args.keep_cloud),
        strict_offline=bool(args.strict_offline),
    )
    artifact_path = build_target(build_opts)

    if as_json:
        _emit_json(
            {
                "ok": True,
                "source": str(source_dir),
                "target": args.target,
                "arch": args.arch,
                "version": resolved_version,
                "artifact": str(artifact_path),
                "fetch": fetch_info,
                "patched_files": len(patch_results) if patch_results is not None else 0,
            }
        )
    else:
        print(f"Built target '{args.target}' ({args.arch}): {artifact_path}")
    return 0


def _cmd_deploy_server(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    options = DeployOptions(
        archive_path=Path(args.archive).expanduser().resolve(),
        t3_home=Path(args.t3_home).expanduser().resolve(),
        bin_dir=Path(args.bin_dir).expanduser().resolve(),
        unit_dir=Path(args.unit_dir).expanduser().resolve() if args.unit_dir else None,
        version=args.version,
        clean_anonymous_id=not bool(args.keep_anonymous_id),
        dry_run=bool(args.dry_run),
        skip_verify=bool(args.skip_verify),
        skip_systemctl=bool(args.skip_systemctl),
    )
    result = deploy_server(options)
    if as_json:
        _emit_json({"ok": True, **result})
    else:
        mode = " (dry-run)" if options.dry_run else ""
        print(f"Deployed server runtime{mode}:")
        for key, value in result.items():
            print(f"  {key}: {value}")
    return 0


def _cmd_rollback_server(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    t3_home = Path(args.t3_home).expanduser().resolve()
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    unit_dir = Path(args.unit_dir).expanduser().resolve() if args.unit_dir else None

    if args.list:
        backups = list_backups(t3_home)
        if as_json:
            _emit_json({"ok": True, "t3_home": str(t3_home), "backups": backups})
        else:
            if not backups:
                print(f"No backups found under {t3_home / 'runtime' / 'no-telemetry-backups'}.")
            else:
                print(f"Backups under {t3_home}:")
                for entry in backups:
                    backup_id = entry.get("backup_id") or entry.get("id") or "unknown"
                    prev_ver = entry.get("previous_version") or entry.get("version") or "unknown"
                    created = entry.get("created_at") or entry.get("timestamp") or ""
                    extra = f" ({created})" if created else ""
                    print(f"  - {backup_id}  version={prev_ver}{extra}")
        return 0

    result = rollback_server(
        t3_home,
        bin_dir,
        unit_dir=unit_dir,
        backup_id=args.backup,
        restore_db=bool(args.restore_db),
        skip_systemctl=bool(args.skip_systemctl),
    )
    if as_json:
        _emit_json({"ok": True, **result})
    else:
        print("Rolled back server runtime:")
        for key, value in result.items():
            print(f"  {key}: {value}")
    return 0


def _cmd_latest_release(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    info = resolve_latest_github_release(repo_url=args.repo_url)
    if as_json:
        _emit_json({"ok": True, **info})
    else:
        print(f"Latest stable release: {info['tag']} ({info['html_url']})")
    return 0


def _cmd_export_aur(args: argparse.Namespace) -> int:
    as_json = _wants_json(args)
    out_dir = Path(args.out).expanduser().resolve()
    source_dir = Path(args.source).expanduser().resolve() if args.source else None
    source_tarball = Path(args.source_tarball).expanduser().resolve() if args.source_tarball else None
    result = export_aur_package(
        repo_root=REPO_ROOT,
        out_dir=out_dir,
        upstream_ref=args.ref,
        pkgrel=args.pkgrel,
        source_dir=source_dir,
        source_tarball=source_tarball,
        source_sha256=args.source_sha256,
        skip_verify=bool(args.skip_verify),
        repo_url=args.repo_url,
    )
    if as_json:
        _emit_json(
            {
                "ok": True,
                "out": str(result.out_dir),
                "ref": result.ref,
                "resolved_ref": result.resolved_ref,
                "pkgver": result.pkgver,
                "pkgrel": result.pkgrel,
                "source_archive": result.source_archive,
                "source_url": result.source_url,
                "source_sha256": result.source_sha256,
                "vp_version": result.vp_version,
                "patch_files": list(result.patch_files),
                "verified_files": result.verified_files,
                "audit_files": list(result.audit_files),
                "files": [str(path) for path in result.files],
            }
        )
    else:
        vp_desc = result.vp_version or "fallback ^1.4.0"
        print(
            f"Exported flat AUR package (ref={result.resolved_ref}, pkgver={result.pkgver}, "
            f"pkgrel={result.pkgrel}, vp={vp_desc}, source_sha256={result.source_sha256}) -> {result.out_dir}"
        )
        for path in result.files:
            print(f"  - {path.name}")
        if result.source_sha256 == "SKIP":
            print(
                "NOTE: Source tarball checksum is SKIP. Omit --source or pass "
                "--source-sha256 <64-hex|auto> / --source-tarball <path> before publishing to AUR.",
                file=sys.stderr,
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t3clean.py",
        description="Build, de-telemetrize, verify, deploy, and package T3 Code without telemetry.",
    )
    _add_json_flag(parser, root=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = subparsers.add_parser(
        "fetch",
        help="Fetch upstream T3 Code source at a pinned tag, branch, or latest-stable.",
    )
    p_fetch.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"Git tag, branch, commit, or 'latest-stable' (default: {DEFAULT_REF}).",
    )
    p_fetch.add_argument(
        "--dest",
        default="./t3code",
        help="Destination directory for the upstream checkout (default: ./t3code).",
    )
    p_fetch.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help=f"Upstream Git repository URL (default: {DEFAULT_REPO_URL}).",
    )
    _add_json_flag(p_fetch)
    p_fetch.set_defaults(handler=_cmd_fetch)

    # patch
    p_patch = subparsers.add_parser(
        "patch",
        help="Apply idempotent de-telemetry patches to a T3 Code source tree.",
    )
    p_patch.add_argument("--source", required=True, help="Path to T3 Code source directory.")
    p_patch.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate patch anchors and report changes without writing files.",
    )
    p_patch.add_argument(
        "--keep-updater",
        action="store_true",
        help="Keep desktop auto-updater policy enabled instead of forcing mode='off'.",
    )
    p_patch.add_argument(
        "--keep-cloud",
        action="store_true",
        help="Keep T3 Connect / Clerk cloud configuration hooks untouched.",
    )
    _add_json_flag(p_patch)
    p_patch.set_defaults(handler=_cmd_patch)

    # verify
    p_verify = subparsers.add_parser(
        "verify",
        help="Scan a source tree, unpacked bundle, or .asar artifact for telemetry leaks.",
    )
    p_verify.add_argument(
        "--artifact",
        required=True,
        help="Path to file or directory to scan (supports streaming .asar inspection).",
    )
    p_verify.add_argument(
        "--keep-updater",
        action="store_true",
        help="Allow app-update.yml when updater was intentionally kept.",
    )
    p_verify.add_argument(
        "--strict-offline",
        action="store_true",
        help="Also flag GitHub raw model/pricing manifest URLs.",
    )
    _add_json_flag(p_verify)
    p_verify.set_defaults(handler=_cmd_verify)

    # build
    p_build = subparsers.add_parser(
        "build",
        help="Fetch, patch, build, and verify a desktop or server artifact.",
    )
    p_build.add_argument(
        "--source",
        default=None,
        help="Path to source directory (default: ./.t3clean-work/t3code).",
    )
    p_build.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"Upstream Git ref if fetching (default: {DEFAULT_REF}).",
    )
    p_build.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help=f"Upstream Git repository URL (default: {DEFAULT_REPO_URL}).",
    )
    p_build.add_argument(
        "--target",
        default="desktop-dir",
        choices=list(VALID_TARGETS),
        help="Build target (default: desktop-dir).",
    )
    p_build.add_argument(
        "--arch",
        default="x64",
        choices=["x64", "arm64"],
        help="Target CPU architecture (default: x64).",
    )
    p_build.add_argument(
        "--version",
        default=None,
        help="Override SemVer build version (default: derived from source + '+notelemetry').",
    )
    p_build.add_argument(
        "--output-dir",
        default=None,
        help="Directory to place built artifacts.",
    )
    p_build.add_argument(
        "--cache-dir",
        default=None,
        help="Optional persistent cache directory for Electron/Cargo/pnpm caches.",
    )
    p_build.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Limit Rust/Rolldown/npm build concurrency (default: auto-throttled on <=16 GiB RAM; also respects T3CLEAN_JOBS).",
    )
    p_build.add_argument(
        "--bootstrap-toolchain",
        action="store_true",
        help="Download isolated official Node + vite-plus into cache if host lacks them.",
    )
    p_build.add_argument(
        "--use-system-electron",
        action="store_true",
        help="Strip bundled Electron binary and stage resources/ for system Electron.",
    )
    p_build.add_argument(
        "--system-electron-bin",
        default="electron",
        help="System Electron executable name when --use-system-electron is set (default: electron).",
    )
    p_build.add_argument(
        "--sourcemap",
        action="store_true",
        help="Enable web sourcemaps during build (default: disabled).",
    )
    p_build.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Use existing --source checkout without running git fetch/clone.",
    )
    p_build.add_argument(
        "--skip-patch",
        action="store_true",
        help="Skip applying de-telemetry patches before building.",
    )
    p_build.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip 'vp install --frozen-lockfile'.",
    )
    p_build.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip post-build telemetry verification.",
    )
    p_build.add_argument(
        "--keep-updater",
        action="store_true",
        help="Keep desktop auto-updater enabled.",
    )
    p_build.add_argument(
        "--keep-cloud",
        action="store_true",
        help="Keep T3 Connect / Clerk cloud integration enabled.",
    )
    p_build.add_argument(
        "--strict-offline",
        action="store_true",
        help="Enable strict-offline checks during post-build verification.",
    )
    _add_json_flag(p_build)
    p_build.set_defaults(handler=_cmd_build)

    # deploy-server
    p_deploy = subparsers.add_parser(
        "deploy-server",
        help="Safely deploy a built server runtime archive with backup and auto-rollback.",
    )
    p_deploy.add_argument(
        "--archive",
        required=True,
        help="Path to server-sea or server-bundle .tar.gz archive.",
    )
    p_deploy.add_argument(
        "--t3-home",
        default="~/.t3",
        help="T3 runtime home directory (default: ~/.t3).",
    )
    p_deploy.add_argument(
        "--bin-dir",
        default="~/.local/bin",
        help="Directory for the 't3' launcher symlink/wrapper (default: ~/.local/bin).",
    )
    p_deploy.add_argument(
        "--unit-dir",
        default=None,
        help="systemd user unit directory (default: ~/.config/systemd/user).",
    )
    p_deploy.add_argument(
        "--version",
        default=None,
        help="Override deployed version directory name.",
    )
    p_deploy.add_argument(
        "--keep-anonymous-id",
        action="store_true",
        help="Do not delete ~/.t3/userdata/anonymous-id during deployment.",
    )
    p_deploy.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate archive and show deployment plan without modifying target files.",
    )
    p_deploy.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip telemetry verification of the extracted archive.",
    )
    p_deploy.add_argument(
        "--skip-systemctl",
        action="store_true",
        help="Skip 'systemctl --user daemon-reload/restart' (for offline/staged deploys).",
    )
    _add_json_flag(p_deploy)
    p_deploy.set_defaults(handler=_cmd_deploy_server)

    # rollback-server
    p_rollback = subparsers.add_parser(
        "rollback-server",
        help="List backups or restore a previous T3 server runtime deployment.",
    )
    p_rollback.add_argument(
        "--t3-home",
        default="~/.t3",
        help="T3 runtime home directory (default: ~/.t3).",
    )
    p_rollback.add_argument(
        "--bin-dir",
        default="~/.local/bin",
        help="Directory containing the 't3' launcher wrapper (default: ~/.local/bin).",
    )
    p_rollback.add_argument(
        "--unit-dir",
        default=None,
        help="systemd user unit directory (default: ~/.config/systemd/user).",
    )
    p_rollback.add_argument(
        "--backup",
        default=None,
        help="Specific backup ID to restore (defaults to latest backup).",
    )
    p_rollback.add_argument(
        "--list",
        action="store_true",
        help="List available backups and exit.",
    )
    p_rollback.add_argument(
        "--restore-db",
        action="store_true",
        help="Also restore backed-up statev2.sqlite (overwrites sessions created after deploy).",
    )
    p_rollback.add_argument(
        "--skip-systemctl",
        action="store_true",
        help="Skip 'systemctl --user daemon-reload/restart'.",
    )
    _add_json_flag(p_rollback)
    p_rollback.set_defaults(handler=_cmd_rollback_server)

    # latest-release
    p_latest = subparsers.add_parser(
        "latest-release",
        help="Query GitHub Releases API for the newest stable upstream T3 Code release.",
    )
    p_latest.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help=f"Upstream GitHub repository URL (default: {DEFAULT_REPO_URL}).",
    )
    _add_json_flag(p_latest)
    p_latest.set_defaults(handler=_cmd_latest_release)

    # export-aur
    p_aur = subparsers.add_parser(
        "export-aur",
        help="Export a minimal, patch-based flat AUR package directory.",
    )
    p_aur.add_argument(
        "--out",
        required=True,
        help="Output directory for flat AUR package files (e.g. ./aur).",
    )
    p_aur.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"Upstream Git ref, 'latest-release', or 'latest-stable' (default: {DEFAULT_REF}).",
    )
    p_aur.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help=f"Upstream GitHub repository URL (default: {DEFAULT_REPO_URL}).",
    )
    p_aur.add_argument(
        "--pkgrel",
        type=int,
        default=1,
        help="Arch package release number (default: 1).",
    )
    p_aur.add_argument(
        "--source",
        default=None,
        help="Generate unified .patch files from an existing clean upstream checkout (mutually exclusive with --source-tarball).",
    )
    p_aur.add_argument(
        "--source-tarball",
        default=None,
        help=(
            "Hash and extract an already-downloaded local upstream .tar.gz archive "
            "(zero network; mutually exclusive with --source and --source-sha256)."
        ),
    )
    p_aur.add_argument(
        "--source-sha256",
        default=None,
        help=(
            "Optional SHA-256 hex digest, 'auto', or 'SKIP' (mainly for --source mode; "
            "omitting --source/--source-tarball downloads and hashes the archive automatically)."
        ),
    )
    p_aur.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip full source-tree telemetry verification after applying generated patches.",
    )
    _add_json_flag(p_aur)
    p_aur.set_defaults(handler=_cmd_export_aur)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = _wants_json(args)
    try:
        return int(args.handler(args))
    except PatchAnchorNotFoundError as exc:
        return _emit_error(f"Patch anchor missing: {exc}", as_json=as_json)
    except (RuntimeError, OSError, ValueError) as exc:
        return _emit_error(str(exc), as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())

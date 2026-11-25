"""Flat AUR package exporter for t3-code-no-telemetry.

Generates a 100% flat, zero-subdirectory directory ready to push directly to AUR:
- t3clean-standalone.py (programmatically combined from __init__.py, patcher.py, verifier.py)
- PKGBUILD (self-contained, zero ../../ references, real sha256sums)
- .SRCINFO (exact match for PKGBUILD metadata & checksums)
- t3-code-no-telemetry.sh
- t3-code-no-telemetry.desktop
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import shutil
import stat

from . import DEFAULT_REF, DEFAULT_REPO_URL

PKGNAME = "t3-code-no-telemetry"
PKGDESC = (
    "T3 Code desktop application built from source with telemetry, "
    "device tracking, and cloud auth stripped"
)
UPSTREAM_URL = "https://github.com/pingdotgg/t3code"

ARCH_DEPENDS: tuple[str, ...] = (
    "alsa-lib",
    "at-spi2-core",
    "cairo",
    "dbus",
    "expat",
    "gdk-pixbuf2",
    "glib2",
    "gtk3",
    "hicolor-icon-theme",
    "libcups",
    "libdbusmenu-glib",
    "libdrm",
    "libsecret",
    "libx11",
    "libxcb",
    "libxcomposite",
    "libxdamage",
    "libxext",
    "libxfixes",
    "libxkbcommon",
    "libxrandr",
    "libxss",
    "libxtst",
    "mesa",
    "nspr",
    "nss",
    "pango",
    "systemd-libs",
    "xdg-utils",
)

ARCH_MAKEDEPENDS: tuple[str, ...] = (
    "git",
    "python",
    "nodejs>=26.12.1",
    "npm",
    "gcc",
    "make",
    "rust",
    "pkgconf",
    "imagemagick",
    "vite-plus>=1.4.0",
)

ARCH_OPTDEPENDS: tuple[str, ...] = (
    "openai-codex: use the system-installed Codex CLI",
)

ARCH_OPTIONS: tuple[str, ...] = ("!strip", "!debug")

_STANDALONE_CLI_FOOTER = '''

# --- BEGIN standalone CLI ---
def _standalone_main(argv: list[str] | None = None) -> int:
    import argparse
    from dataclasses import asdict
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="t3clean-standalone.py",
        description="Standalone de-telemetry patcher and verifier for T3 Code AUR builds.",
    )
    parser.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_patch = subparsers.add_parser("patch", help="Apply idempotent de-telemetry patches.")
    p_patch.add_argument("--source", required=True, help="Path to T3 Code source tree.")
    p_patch.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    p_patch.add_argument("--keep-updater", action="store_true", help="Keep desktop updater enabled.")
    p_patch.add_argument("--keep-cloud", action="store_true", help="Keep cloud config hooks.")
    p_patch.add_argument("--json", dest="json_sub", action="store_true", default=False)

    p_verify = subparsers.add_parser("verify", help="Verify artifact or source tree has no telemetry.")
    p_verify.add_argument("--artifact", required=True, help="Path to file or directory to scan.")
    p_verify.add_argument("--keep-updater", action="store_true", help="Allow app-update.yml.")
    p_verify.add_argument(
        "--strict-offline",
        action="store_true",
        help="Also flag GitHub raw manifest URLs.",
    )
    p_verify.add_argument("--json", dest="json_sub", action="store_true", default=False)

    args = parser.parse_args(argv)
    as_json = bool(args.json or getattr(args, "json_sub", False))

    try:
        if args.command == "patch":
            source_dir = Path(args.source).expanduser().resolve()
            options = PatchOptions(
                keep_updater=bool(args.keep_updater),
                keep_cloud=bool(args.keep_cloud),
                dry_run=bool(args.dry_run),
            )
            results = patch_source_tree(source_dir, options)
            if as_json:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "source": str(source_dir),
                            "dry_run": options.dry_run,
                            "results": [asdict(r) for r in results],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for item in results:
                    tag = (
                        "ALREADY"
                        if (item.already_patched and not item.changed)
                        else ("DRY-RUN" if (item.changed and options.dry_run) else ("PATCHED" if item.changed else "SKIPPED"))
                    )
                    print(f"[{tag:7s}] {item.rule_id}: {item.relative_path} — {item.summary}")
            return 0

        if args.command == "verify":
            target_path = Path(args.artifact).expanduser().resolve()
            options = VerifyOptions(
                keep_updater=bool(args.keep_updater),
                strict_offline=bool(args.strict_offline),
            )
            report = verify_artifact(target_path, options)
            if as_json:
                print(
                    json.dumps(
                        {
                            "ok": report.ok,
                            "artifact": str(target_path),
                            "scanned_files": report.scanned_files,
                            "findings": [asdict(f) for f in report.findings],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif report.ok:
                print(f"OK: verified {report.scanned_files} file(s) with 0 findings.")
            else:
                print(
                    f"FAILED: {len(report.findings)} finding(s) across {report.scanned_files} file(s):",
                    file=sys.stderr,
                )
                for finding in report.findings:
                    print(f"  - [{finding.rule}] {finding.path}: {finding.detail}", file=sys.stderr)
            return 0 if report.ok else 1
    except PatchAnchorNotFoundError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": f"Patch anchor missing: {exc}"}))
        else:
            print(f"ERROR: Patch anchor missing: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, ValueError) as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(_standalone_main())
'''


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_module_for_bundle(source: str, label: str) -> tuple[set[str], str]:
    """Strip __future__ imports, relative/package imports, __all__, and __main__ blocks."""
    tree = ast.parse(source, filename=label)
    lines = source.splitlines(keepends=True)
    future_features: set[str] = set()
    remove_ranges: list[tuple[int, int]] = []

    for idx, node in enumerate(tree.body):
        # Remove top-level module docstring so the bundled script stays tidy.
        if (
            idx == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.end_lineno is not None
        ):
            remove_ranges.append((node.lineno, node.end_lineno))
            continue

        if isinstance(node, ast.ImportFrom) and node.end_lineno is not None:
            if node.module == "__future__":
                for alias in node.names:
                    future_features.add(alias.name)
                remove_ranges.append((node.lineno, node.end_lineno))
                continue
            if node.level > 0 or node.module == "t3clean" or (node.module or "").startswith("t3clean."):
                remove_ranges.append((node.lineno, node.end_lineno))
                continue

        if isinstance(node, ast.Import) and node.end_lineno is not None:
            if all(alias.name == "t3clean" or alias.name.startswith("t3clean.") for alias in node.names):
                remove_ranges.append((node.lineno, node.end_lineno))
                continue

        if isinstance(node, ast.Assign) and node.end_lineno is not None:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "__all__":
                remove_ranges.append((node.lineno, node.end_lineno))
                continue

        if isinstance(node, ast.If) and node.end_lineno is not None:
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                remove_ranges.append((node.lineno, node.end_lineno))

    skip_lines: set[int] = set()
    for start, end in remove_ranges:
        for line_no in range(start, end + 1):
            skip_lines.add(line_no)

    kept = [line for line_no, line in enumerate(lines, start=1) if line_no not in skip_lines]
    body = "".join(kept).strip()
    return future_features, body


def _build_standalone_script(repo_root: Path) -> str:
    """Programmatically combine __init__.py, patcher.py, and verifier.py into one CLI."""
    pkg_dir = repo_root / "t3clean"
    parts = [
        ("t3clean/__init__.py", pkg_dir / "__init__.py"),
        ("t3clean/patcher.py", pkg_dir / "patcher.py"),
        ("t3clean/verifier.py", pkg_dir / "verifier.py"),
    ]
    missing = [str(path) for _, path in parts if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required source module(s) missing for AUR export: {', '.join(missing)}")

    all_futures: set[str] = {"annotations"}
    sections: list[str] = []
    for label, path in parts:
        futures, cleaned = _strip_module_for_bundle(path.read_text(encoding="utf-8"), label)
        all_futures.update(futures)
        sections.append(f"# --- BEGIN {label} ---\n{cleaned}\n# --- END {label} ---")

    future_block = "\n".join(f"from __future__ import {feat}" for feat in sorted(all_futures))
    header = (
        "#!/usr/bin/env python3\n"
        '"""Auto-generated standalone de-telemetry patcher & verifier for AUR builds.\n'
        "\n"
        "Combined from t3clean/__init__.py, t3clean/patcher.py, and t3clean/verifier.py.\n"
        'Pure Python 3.10+ standard library only.\n'
        '"""\n\n'
        f"{future_block}\n\n"
        "from pathlib import Path\n"
    )
    combined = header + "\n\n".join(sections) + "\n" + _STANDALONE_CLI_FOOTER.lstrip("\n")

    # Validate syntax before emitting.
    compile(combined, "t3clean-standalone.py", "exec")
    return combined


def _git_fragment_for_ref(ref: str) -> str:
    cleaned = ref.strip()
    if re.match(r"^v[0-9]", cleaned):
        return f"#tag={cleaned}"
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", cleaned):
        return f"#commit={cleaned}"
    return f"#branch={cleaned}"


def _pkgver_for_ref(ref: str) -> str:
    cleaned = ref.strip()
    if re.match(r"^v?\d+\.\d+\.\d+", cleaned):
        ver = cleaned.lstrip("v")
        ver = re.sub(r"[^A-Za-z0-9._]+", ".", ver).strip(".")
        return ver
    fallback_base = DEFAULT_REF.lstrip("v")
    safe_ref = re.sub(r"[^A-Za-z0-9._]+", ".", cleaned).strip(".")
    return f"{fallback_base}.{safe_ref}" if safe_ref else fallback_base


def _format_bash_array(name: str, items: tuple[str, ...] | list[str]) -> str:
    rendered = "\n".join(f"  '{item}'" for item in items)
    return f"{name}=(\n{rendered}\n)"


def _render_flat_pkgbuild(
    *,
    upstream_ref: str,
    pkgver: str,
    pkgrel: int,
    sha_standalone: str,
    sha_launcher: str,
    sha_desktop: str,
) -> str:
    git_fragment = _git_fragment_for_ref(upstream_ref)
    depends_block = _format_bash_array("depends", ARCH_DEPENDS)
    makedepends_block = _format_bash_array("makedepends", ARCH_MAKEDEPENDS)
    optdepends_block = _format_bash_array("optdepends", ARCH_OPTDEPENDS)
    options_str = " ".join(f"'{opt}'" for opt in ARCH_OPTIONS)

    return f"""# Maintainer: Zhang Ke
# Auto-generated flat AUR PKGBUILD by t3clean.py export-aur

pkgname={PKGNAME}
pkgver={pkgver}
pkgrel={pkgrel}
pkgdesc='{PKGDESC}'
arch=('x86_64' 'aarch64')
url='{UPSTREAM_URL}'
license=('MIT')
options=({options_str})

# Pinned upstream ref: {upstream_ref}
# (Re-run `python3 t3clean.py export-aur --ref <ref>` to switch refs for AUR uploads.)
# Optional build switches (all disabled by default for reproducible AUR builds)
_build_deb="${{T3CLEAN_BUILD_DEB:-0}}"
_use_system_electron="${{T3CLEAN_USE_SYSTEM_ELECTRON:-0}}"
_system_electron_bin="${{T3CLEAN_SYSTEM_ELECTRON_BIN:-electron}}"
_keep_updater="${{T3CLEAN_KEEP_UPDATER:-0}}"
_keep_cloud="${{T3CLEAN_KEEP_CLOUD:-0}}"
_strict_offline="${{T3CLEAN_STRICT_OFFLINE:-0}}"

{depends_block}
if [[ "${{_use_system_electron}}" == "1" ]]; then
  depends+=("${{_system_electron_bin}}")
fi

{makedepends_block}
{optdepends_block}

# Coexists cleanly with t3code-bin (no conflicts/provides overlap).
source=(
  "t3code::git+{DEFAULT_REPO_URL}{git_fragment}"
  "t3clean-standalone.py"
  "t3-code-no-telemetry.sh"
  "t3-code-no-telemetry.desktop"
)
sha256sums=(
  'SKIP'
  '{sha_standalone}'
  '{sha_launcher}'
  '{sha_desktop}'
)

_t3clean_arch() {{
  case "${{CARCH}}" in
    x86_64)  printf 'x64' ;;
    aarch64) printf 'arm64' ;;
    *)       printf 'x64' ;;
  esac
}}

_t3clean_semver() {{
  if [[ "${{pkgver}}" =~ ^([0-9]+\\.[0-9]+\\.[0-9]+)\\.(.+)$ ]]; then
    printf '%s-%s+notelemetry' "${{BASH_REMATCH[1]}}" "${{BASH_REMATCH[2]}}"
  else
    printf '%s+notelemetry' "${{pkgver}}"
  fi
}}

_t3clean_cache_dir() {{
  if [[ -n "${{T3CLEAN_CACHE_DIR:-}}" ]]; then
    printf '%s' "${{T3CLEAN_CACHE_DIR}}"
  else
    printf '%s' "${{srcdir}}/.cache"
  fi
}}

_t3clean_scrub_env() {{
  local _var
  while IFS='=' read -r _var _; do
    case "${{_var}}" in
      T3CODE_CLERK_*|VITE_CLERK_*|EXPO_PUBLIC_CLERK_*|T3CODE_RELAY_*|VITE_T3CODE_RELAY_*|VITE_RELAY_OTLP_*|T3CODE_MOBILE_OTLP_*|EXPO_PUBLIC_OTLP_*|T3CODE_POSTHOG_*)
        unset "${{_var}}"
        ;;
    esac
  done < <(env)
  unset T3CODE_DESKTOP_UPDATE_REPOSITORY GITHUB_REPOSITORY CSC_LINK CSC_KEY_PASSWORD

  export T3CODE_TELEMETRY_ENABLED=false
  export DO_NOT_TRACK=1
  export T3CODE_TELEMETRY_CHANNEL=dev
  export T3CODE_WEB_SOURCEMAP=false

  local _cache_dir
  _cache_dir="$(_t3clean_cache_dir)"
  mkdir -p "${{_cache_dir}}/electron" "${{_cache_dir}}/electron-builder" "${{_cache_dir}}/cargo" "${{_cache_dir}}/pnpm" "${{_cache_dir}}/pnpm-store" "${{_cache_dir}}/xdg"
  export ELECTRON_CACHE="${{_cache_dir}}/electron"
  export ELECTRON_BUILDER_CACHE="${{_cache_dir}}/electron-builder"
  export CARGO_HOME="${{_cache_dir}}/cargo"
  export PNPM_HOME="${{_cache_dir}}/pnpm"
  export npm_config_store_dir="${{_cache_dir}}/pnpm-store"
  export XDG_CACHE_HOME="${{_cache_dir}}/xdg"
}}

pkgver() {{
  cd "${{srcdir}}/t3code"
  local desc rest count hash tag

  desc=$(git describe --long --tags --abbrev=7 --match='v[0-9]*' --exclude='*nightly*' --exclude='*preview*' 2>/dev/null) || {{
    printf '0.0.0.r%s.g%s' "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
    return
  }}

  hash=${{desc##*-g}}
  rest=${{desc%-g*}}
  count=${{rest##*-}}
  tag=${{rest%-${{count}}}}
  tag=${{tag%-}}
  tag=${{tag#v}}
  tag=${{tag//-/.}}

  if [[ "${{count}}" == "0" ]]; then
    printf '%s' "${{tag}}"
  else
    printf '%s.r%s.g%s' "${{tag}}" "${{count}}" "${{hash}}"
  fi
}}

prepare() {{
  local _patch_args=(--source "${{srcdir}}/t3code")
  if [[ "${{_keep_updater}}" == "1" ]]; then
    _patch_args+=(--keep-updater)
  fi
  if [[ "${{_keep_cloud}}" == "1" ]]; then
    _patch_args+=(--keep-cloud)
  fi
  python3 "${{srcdir}}/t3clean-standalone.py" patch "${{_patch_args[@]}}"
}}

build() {{
  cd "${{srcdir}}/t3code"
  rm -f .env .env.local
  _t3clean_scrub_env

  local _arch _build_ver
  _arch="$(_t3clean_arch)"
  _build_ver="$(_t3clean_semver)"

  vp install --frozen-lockfile
  node scripts/build-desktop-artifact.ts \\
    --platform linux \\
    --target dir \\
    --arch "${{_arch}}" \\
    --build-version "${{_build_ver}}" \\
    --output-dir "${{srcdir}}/out"

  if [[ "${{_use_system_electron}}" == "1" ]]; then
    local _unpacked
    _unpacked="$(find "${{srcdir}}/out" -maxdepth 1 -type d -name 'linux*-unpacked' -print -quit)"
    if [[ -z "${{_unpacked}}" || ! -d "${{_unpacked}}/resources" ]]; then
      echo "ERROR: Expected linux*-unpacked/resources under ${{srcdir}}/out" >&2
      return 1
    fi
    rm -rf "${{srcdir}}/out/linux-system-electron"
    install -d "${{srcdir}}/out/linux-system-electron"
    cp -a "${{_unpacked}}/resources" "${{srcdir}}/out/linux-system-electron/resources"
    echo "NOTE: Staged resources-only payload for system Electron (${{_system_electron_bin}}); ensure native addon ABI matches." >&2
  fi

  if [[ "${{_build_deb}}" == "1" ]]; then
    local _deb_out="${{T3CLEAN_DEB_OUT_DIR:-${{srcdir}}/out-deb}}"
    node scripts/build-desktop-artifact.ts \\
      --platform linux \\
      --target deb \\
      --arch "${{_arch}}" \\
      --build-version "${{_build_ver}}" \\
      --skip-build \\
      --output-dir "${{_deb_out}}"
  fi
}}

check() {{
  local _artifact_dir
  if [[ "${{_use_system_electron}}" == "1" ]]; then
    _artifact_dir="${{srcdir}}/out/linux-system-electron"
  else
    _artifact_dir="$(find "${{srcdir}}/out" -maxdepth 1 -type d -name 'linux*-unpacked' -print -quit)"
  fi
  if [[ -z "${{_artifact_dir}}" || ! -d "${{_artifact_dir}}" ]]; then
    echo "ERROR: Built desktop artifact directory not found under ${{srcdir}}/out" >&2
    return 1
  fi

  local _verify_args=(--artifact "${{_artifact_dir}}")
  if [[ "${{_keep_updater}}" == "1" ]]; then
    _verify_args+=(--keep-updater)
  fi
  if [[ "${{_strict_offline}}" == "1" ]]; then
    _verify_args+=(--strict-offline)
  fi
  python3 "${{srcdir}}/t3clean-standalone.py" verify "${{_verify_args[@]}}"
}}

package() {{
  local _payload_dir
  if [[ "${{_use_system_electron}}" == "1" ]]; then
    _payload_dir="${{srcdir}}/out/linux-system-electron"
  else
    _payload_dir="$(find "${{srcdir}}/out" -maxdepth 1 -type d -name 'linux*-unpacked' -print -quit)"
  fi
  if [[ -z "${{_payload_dir}}" || ! -d "${{_payload_dir}}" ]]; then
    echo "ERROR: Packaged payload directory missing under ${{srcdir}}/out" >&2
    return 1
  fi

  install -d "${{pkgdir}}/usr/lib/${{pkgname}}"
  cp -a "${{_payload_dir}}/." "${{pkgdir}}/usr/lib/${{pkgname}}/"
  if [[ "${{_use_system_electron}}" == "1" ]]; then
    printf '%s\\n' "${{_system_electron_bin}}" > "${{pkgdir}}/usr/lib/${{pkgname}}/.use-system-electron"
  fi
  chmod -R a+rX "${{pkgdir}}/usr/lib/${{pkgname}}"

  install -Dm755 "${{srcdir}}/t3-code-no-telemetry.sh" "${{pkgdir}}/usr/bin/${{pkgname}}"
  install -Dm644 "${{srcdir}}/t3-code-no-telemetry.desktop" \\
    "${{pkgdir}}/usr/share/applications/${{pkgname}}.desktop"

  local _icon_src="${{srcdir}}/t3code/assets/prod/black-universal-1024.png"
  if [[ -f "${{_icon_src}}" ]]; then
    local _resize_tool=""
    if command -v magick >/dev/null 2>&1; then
      _resize_tool="magick"
    elif command -v convert >/dev/null 2>&1; then
      _resize_tool="convert"
    fi
    local _size
    for _size in 16 22 24 32 48 64 128 256 512; do
      install -d "${{pkgdir}}/usr/share/icons/hicolor/${{_size}}x${{_size}}/apps"
      if [[ -n "${{_resize_tool}}" ]]; then
        "${{_resize_tool}}" "${{_icon_src}}" -resize "${{_size}}x${{_size}}" \\
          "${{pkgdir}}/usr/share/icons/hicolor/${{_size}}x${{_size}}/apps/${{pkgname}}.png"
      elif [[ "${{_size}}" == "512" ]]; then
        install -Dm644 "${{_icon_src}}" \\
          "${{pkgdir}}/usr/share/icons/hicolor/${{_size}}x${{_size}}/apps/${{pkgname}}.png"
      fi
    done
    if [[ -f "${{pkgdir}}/usr/share/icons/hicolor/512x512/apps/${{pkgname}}.png" ]]; then
      install -Dm644 "${{pkgdir}}/usr/share/icons/hicolor/512x512/apps/${{pkgname}}.png" \\
        "${{pkgdir}}/usr/share/pixmaps/${{pkgname}}.png"
    else
      install -Dm644 "${{_icon_src}}" "${{pkgdir}}/usr/share/pixmaps/${{pkgname}}.png"
    fi
  fi

  install -Dm644 "${{srcdir}}/t3code/LICENSE" "${{pkgdir}}/usr/share/licenses/${{pkgname}}/LICENSE"
}}
"""


def _render_srcinfo(
    *,
    upstream_ref: str,
    pkgver: str,
    pkgrel: int,
    sha_standalone: str,
    sha_launcher: str,
    sha_desktop: str,
) -> str:
    git_fragment = _git_fragment_for_ref(upstream_ref)
    lines: list[str] = [
        f"pkgbase = {PKGNAME}",
        f"\tpkgdesc = {PKGDESC}",
        f"\tpkgver = {pkgver}",
        f"\tpkgrel = {pkgrel}",
        f"\turl = {UPSTREAM_URL}",
        "\tarch = x86_64",
        "\tarch = aarch64",
        "\tlicense = MIT",
    ]
    for dep in ARCH_MAKEDEPENDS:
        lines.append(f"\tmakedepends = {dep}")
    for dep in ARCH_DEPENDS:
        lines.append(f"\tdepends = {dep}")
    for opt in ARCH_OPTDEPENDS:
        lines.append(f"\toptdepends = {opt}")
    for opt in ARCH_OPTIONS:
        lines.append(f"\toptions = {opt}")
    lines.extend(
        [
            f"\tsource = t3code::git+{DEFAULT_REPO_URL}{git_fragment}",
            "\tsource = t3clean-standalone.py",
            "\tsource = t3-code-no-telemetry.sh",
            "\tsource = t3-code-no-telemetry.desktop",
            "\tsha256sums = SKIP",
            f"\tsha256sums = {sha_standalone}",
            f"\tsha256sums = {sha_launcher}",
            f"\tsha256sums = {sha_desktop}",
            "",
            f"pkgname = {PKGNAME}",
            "",
        ]
    )
    return "\n".join(lines)


def export_aur_package(
    repo_root: Path,
    out_dir: Path,
    *,
    upstream_ref: str = DEFAULT_REF,
    pkgrel: int = 1,
) -> list[Path]:
    """Generate a 100% flat, zero-subdirectory AUR package directory.

    Returns the list of created top-level file paths inside ``out_dir``.
    """
    if pkgrel < 1:
        raise ValueError(f"pkgrel must be >= 1, got {pkgrel}")
    if not upstream_ref or not upstream_ref.strip():
        raise ValueError("upstream_ref must be a non-empty Git ref")

    repo_root = Path(repo_root).resolve()
    out_dir = Path(out_dir).resolve()
    arch_dir = repo_root / "packaging" / "arch"
    src_launcher = arch_dir / "t3-code-no-telemetry.sh"
    src_desktop = arch_dir / "t3-code-no-telemetry.desktop"

    for required in (src_launcher, src_desktop):
        if not required.is_file():
            raise FileNotFoundError(f"Missing packaging asset required for AUR export: {required}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate standalone patcher + verifier CLI.
    standalone_path = out_dir / "t3clean-standalone.py"
    standalone_code = _build_standalone_script(repo_root)
    standalone_path.write_text(standalone_code, encoding="utf-8")
    standalone_path.chmod(standalone_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 2. Copy launcher script & desktop entry flat into out_dir.
    launcher_path = out_dir / "t3-code-no-telemetry.sh"
    desktop_path = out_dir / "t3-code-no-telemetry.desktop"
    shutil.copyfile(src_launcher, launcher_path)
    launcher_path.chmod(launcher_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copyfile(src_desktop, desktop_path)

    # 3. Compute exact SHA-256 sums for all local AUR files.
    sha_standalone = _sha256_file(standalone_path)
    sha_launcher = _sha256_file(launcher_path)
    sha_desktop = _sha256_file(desktop_path)

    pkgver = _pkgver_for_ref(upstream_ref)

    # 4. Write flat PKGBUILD and matching .SRCINFO.
    pkgbuild_path = out_dir / "PKGBUILD"
    pkgbuild_path.write_text(
        _render_flat_pkgbuild(
            upstream_ref=upstream_ref.strip(),
            pkgver=pkgver,
            pkgrel=pkgrel,
            sha_standalone=sha_standalone,
            sha_launcher=sha_launcher,
            sha_desktop=sha_desktop,
        ),
        encoding="utf-8",
    )

    srcinfo_path = out_dir / ".SRCINFO"
    srcinfo_path.write_text(
        _render_srcinfo(
            upstream_ref=upstream_ref.strip(),
            pkgver=pkgver,
            pkgrel=pkgrel,
            sha_standalone=sha_standalone,
            sha_launcher=sha_launcher,
            sha_desktop=sha_desktop,
        ),
        encoding="utf-8",
    )

    return [
        pkgbuild_path,
        srcinfo_path,
        standalone_path,
        launcher_path,
        desktop_path,
    ]

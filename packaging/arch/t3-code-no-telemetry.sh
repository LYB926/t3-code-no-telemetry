#!/usr/bin/env bash
set -euo pipefail

# Defense-in-depth telemetry suppression
export T3CODE_TELEMETRY_ENABLED=false
export DO_NOT_TRACK=1

if [[ -z "${CODEX_CLI_PATH-}" ]] && command -v codex >/dev/null 2>&1; then
  export CODEX_CLI_PATH="$(command -v codex)"
fi

extra_flags=()
if [[ -n "${WAYLAND_DISPLAY-}" || "${XDG_SESSION_TYPE-}" == "wayland" ]]; then
  extra_flags+=(--enable-features=UseOzonePlatform --ozone-platform=wayland --ozone-platform-hint=wayland)
else
  extra_flags+=(--ozone-platform-hint=auto)
fi

if [[ -f /usr/lib/t3-code-no-telemetry/.use-system-electron ]]; then
  electron_bin="$(tr -d '[:space:]' < /usr/lib/t3-code-no-telemetry/.use-system-electron)"
  electron_bin="${T3CLEAN_SYSTEM_ELECTRON_BIN:-${electron_bin:-electron}}"
  if ! command -v "${electron_bin}" >/dev/null 2>&1; then
    printf 'ERROR: System Electron binary "%s" not found in PATH.\n' "${electron_bin}" >&2
    exit 1
  fi
  exec "${electron_bin}" /usr/lib/t3-code-no-telemetry/resources/app.asar "${extra_flags[@]}" "$@"
fi

exec /usr/lib/t3-code-no-telemetry/t3code "${extra_flags[@]}" "$@"

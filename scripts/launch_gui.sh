#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
LAUNCHER="$APP_ROOT/.venv/bin/pc-agent-gui"

if [ ! -x "$LAUNCHER" ]; then
  printf '%s\n' "The GUI environment is not installed at: $APP_ROOT" >&2
  printf '%s\n' "Run scripts/install_posix.sh from the package first." >&2
  exit 1
fi

cd "$APP_ROOT"
exec "$LAUNCHER" "$@"

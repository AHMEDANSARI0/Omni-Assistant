#!/bin/sh
# Self-Healing PC Agent per-user POSIX installer.
#
# Run this file from the generated package root, or from scripts/ inside an
# already copied application tree to set that tree up in place. It never
# uses administrator elevation, downloads Python, or edits the real .env.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=""
SKIP_PYTHON_SETUP=0
FORCE=0

usage() {
  printf '%s\n' "Usage: sh install_posix.sh [--prefix DIR] [--skip-python-setup] [--force]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      if [ "$#" -lt 2 ]; then
        printf '%s\n' "--prefix requires a directory." >&2
        exit 2
      fi
      PREFIX=$2
      shift 2
      ;;
    --skip-python-setup)
      SKIP_PYTHON_SETUP=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '%s\n' "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "${HOME:-}" ]; then
  printf '%s\n' "HOME is not set; cannot choose a per-user install location." >&2
  exit 1
fi

PACKAGE_MODE=0
PACKAGE_ROOT=""
if [ -d "$SCRIPT_DIR/payload" ] && [ -f "$SCRIPT_DIR/payload-files.txt" ]; then
  PACKAGE_MODE=1
  PACKAGE_ROOT=$SCRIPT_DIR
  SOURCE_DIR="$SCRIPT_DIR/payload"
elif [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
  SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
else
  printf '%s\n' "Could not locate the POSIX package payload or application tree." >&2
  exit 1
fi

OS_NAME=$(uname -s)
if [ -z "$PREFIX" ]; then
  if [ "$PACKAGE_MODE" -eq 0 ]; then
    TARGET_DIR=$SOURCE_DIR
  elif [ "$OS_NAME" = "Darwin" ]; then
    TARGET_DIR="$HOME/Library/Application Support/Self-Healing PC Agent"
  else
    TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/self-healing-pc-agent"
  fi
else
  if [ "$PACKAGE_MODE" -eq 0 ]; then
    printf '%s\n' "An in-place scripts/install_posix.sh does not accept --prefix." >&2
    printf '%s\n' "Use the generated package root install.sh to choose a prefix." >&2
    exit 2
  fi
  case "$PREFIX" in
    /*) TARGET_DIR=$PREFIX ;;
    *) TARGET_DIR=$(pwd)/$PREFIX ;;
  esac
fi

if [ -e "$TARGET_DIR" ] && [ ! -d "$TARGET_DIR" ]; then
  printf '%s\n' "Install target exists but is not a directory: $TARGET_DIR" >&2
  exit 1
fi

if [ "$PACKAGE_MODE" -eq 1 ] && [ -d "$TARGET_DIR" ]; then
  EXISTING=$(ls -A "$TARGET_DIR" 2>/dev/null || true)
  if [ -n "$EXISTING" ] && [ ! -f "$TARGET_DIR/.pc-agent-posix-install.txt" ] && [ ! -f "$TARGET_DIR/pyproject.toml" ] && [ "$FORCE" -ne 1 ]; then
    printf '%s\n' "Install target is a non-empty directory that does not look like this app:" >&2
    printf '%s\n' "  $TARGET_DIR" >&2
    printf '%s\n' "Choose another --prefix, or pass --force only if overlaying it is intentional." >&2
    exit 1
  fi
fi

mkdir -p "$TARGET_DIR"
TARGET_DIR=$(CDPATH= cd -- "$TARGET_DIR" && pwd)
SOURCE_DIR=$(CDPATH= cd -- "$SOURCE_DIR" && pwd)

if [ "$PACKAGE_MODE" -eq 1 ] && [ "$TARGET_DIR" != "$SOURCE_DIR" ]; then
  cp -R "$SOURCE_DIR/." "$TARGET_DIR/"
  cp "$PACKAGE_ROOT/payload-files.txt" "$TARGET_DIR/.pc-agent-posix-files.txt"
elif [ "$PACKAGE_MODE" -eq 0 ] && [ ! -f "$TARGET_DIR/.pc-agent-posix-files.txt" ]; then
  # In a source checkout there is no publisher payload list. Record only the
  # installer-owned POSIX scripts so uninstall never guesses app files.
  printf '%s\n' \
    "scripts/install_posix.sh" \
    "scripts/launch_cli.sh" \
    "scripts/launch_gui.sh" \
    "scripts/uninstall_posix.sh" \
    > "$TARGET_DIR/.pc-agent-posix-files.txt"
fi

mkdir -p "$TARGET_DIR/data" "$TARGET_DIR/workspace"
chmod +x \
  "$TARGET_DIR/scripts/install_posix.sh" \
  "$TARGET_DIR/scripts/launch_cli.sh" \
  "$TARGET_DIR/scripts/launch_gui.sh" \
  "$TARGET_DIR/scripts/uninstall_posix.sh"

VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$TARGET_DIR/pyproject.toml" | head -n 1)
if [ -z "$VERSION" ]; then
  VERSION="unknown"
fi

MANIFEST_SHA256="unavailable"
if [ "$PACKAGE_MODE" -eq 1 ] && [ -f "$PACKAGE_ROOT/posix-package-manifest.json" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    MANIFEST_SHA256=$(sha256sum "$PACKAGE_ROOT/posix-package-manifest.json" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then
    MANIFEST_SHA256=$(shasum -a 256 "$PACKAGE_ROOT/posix-package-manifest.json" | awk '{print $1}')
  fi
fi

printf '%s\n' \
  "format=1" \
  "app=self-healing-pc-agent" \
  "version=$VERSION" \
  "manifest_sha256=$MANIFEST_SHA256" \
  > "$TARGET_DIR/.pc-agent-posix-install.txt"

if [ "$SKIP_PYTHON_SETUP" -eq 0 ]; then
  PYTHON_BIN=${PYTHON_BIN:-python3}
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf '%s\n' "Python 3.11 or newer was not found. Set PYTHON_BIN to its path." >&2
    exit 1
  fi
  if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    printf '%s\n' "Python 3.11 or newer is required." >&2
    exit 1
  fi
  if [ ! -x "$TARGET_DIR/.venv/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$TARGET_DIR/.venv"
  fi
  "$TARGET_DIR/.venv/bin/python" -m pip install -e "$TARGET_DIR"
  (cd "$TARGET_DIR" && ./.venv/bin/pc-agent doctor >/dev/null)
  (cd "$TARGET_DIR" && ./.venv/bin/pc-agent gui --check >/dev/null)
else
  printf '%s\n' "Python setup skipped by request; launchers will work after .venv is created."
fi

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

CLI_WRAPPER="$BIN_DIR/self-healing-pc-agent"
GUI_WRAPPER="$BIN_DIR/self-healing-pc-agent-gui"

{
  printf '%s\n' "#!/bin/sh"
  printf 'exec %s "$@"\n' "$(shell_quote "$TARGET_DIR/scripts/launch_cli.sh")"
} > "$CLI_WRAPPER"
{
  printf '%s\n' "#!/bin/sh"
  printf 'exec %s "$@"\n' "$(shell_quote "$TARGET_DIR/scripts/launch_gui.sh")"
} > "$GUI_WRAPPER"
chmod +x "$CLI_WRAPPER" "$GUI_WRAPPER"

if [ "$OS_NAME" = "Darwin" ]; then
  APPLICATIONS_DIR="$HOME/Applications"
  mkdir -p "$APPLICATIONS_DIR"
  COMMAND_FILE="$APPLICATIONS_DIR/Self-Healing PC Agent.command"
  {
    printf '%s\n' "#!/bin/sh"
    printf 'exec %s "$@"\n' "$(shell_quote "$GUI_WRAPPER")"
  } > "$COMMAND_FILE"
  chmod +x "$COMMAND_FILE"
  printf '%s\n' "macOS launcher: $COMMAND_FILE"
else
  APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$APPLICATIONS_DIR"
  DESKTOP_ESCAPED=$(printf '%s' "$GUI_WRAPPER" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\$/\\$/g; s/`/\\`/g')
  {
    printf '%s\n' "[Desktop Entry]"
    printf '%s\n' "Type=Application"
    printf '%s\n' "Name=Self-Healing PC Agent"
    printf 'Exec="%s"\n' "$DESKTOP_ESCAPED"
    printf '%s\n' "Terminal=false"
    printf '%s\n' "Categories=Utility;"
  } > "$APPLICATIONS_DIR/self-healing-pc-agent.desktop"
  printf '%s\n' "Desktop entry: $APPLICATIONS_DIR/self-healing-pc-agent.desktop"
fi

printf '%s\n' "Self-Healing PC Agent $VERSION installed at: $TARGET_DIR"
printf '%s\n' "CLI launcher: $CLI_WRAPPER"
printf '%s\n' "GUI launcher: $GUI_WRAPPER"
printf '%s\n' "User data, workspace files, and .env were preserved."

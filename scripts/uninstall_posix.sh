#!/bin/sh
# Conservative per-user POSIX uninstaller for Self-Healing PC Agent.
# By default it removes only launchers and the generated .venv. Application
# payload files are removed only with --remove-app --yes, using the exact
# publisher file list. User .env, data, and workspace files are preserved.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=""
REMOVE_APP=0
ASSUME_YES=0

usage() {
  printf '%s\n' "Usage: sh uninstall_posix.sh [--prefix DIR] [--remove-app --yes]"
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
    --remove-app)
      REMOVE_APP=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
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
  printf '%s\n' "HOME is not set; cannot locate the per-user installation." >&2
  exit 1
fi

OS_NAME=$(uname -s)
if [ -n "$PREFIX" ]; then
  case "$PREFIX" in
    /*) TARGET_DIR=$PREFIX ;;
    *) TARGET_DIR=$(pwd)/$PREFIX ;;
  esac
elif [ -f "$SCRIPT_DIR/../.pc-agent-posix-install.txt" ]; then
  TARGET_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
elif [ "$OS_NAME" = "Darwin" ]; then
  TARGET_DIR="$HOME/Library/Application Support/Self-Healing PC Agent"
else
  TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/self-healing-pc-agent"
fi

if [ ! -d "$TARGET_DIR" ]; then
  printf '%s\n' "No POSIX installation directory was found at: $TARGET_DIR" >&2
  exit 1
fi
TARGET_DIR=$(CDPATH= cd -- "$TARGET_DIR" && pwd)

MARKER="$TARGET_DIR/.pc-agent-posix-install.txt"
FILE_LIST="$TARGET_DIR/.pc-agent-posix-files.txt"
if [ ! -f "$MARKER" ] && [ ! -f "$FILE_LIST" ]; then
  printf '%s\n' "Refusing to uninstall a directory without the POSIX install marker:" >&2
  printf '%s\n' "  $TARGET_DIR" >&2
  exit 1
fi

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
rm -f "$BIN_DIR/self-healing-pc-agent" "$BIN_DIR/self-healing-pc-agent-gui"
if [ "$OS_NAME" = "Darwin" ]; then
  rm -f "$HOME/Applications/Self-Healing PC Agent.command"
else
  APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  rm -f "$APPLICATIONS_DIR/self-healing-pc-agent.desktop"
fi
printf '%s\n' "Removed POSIX launchers for: $TARGET_DIR"

if [ -d "$TARGET_DIR/.venv" ]; then
  VENV_DIR=$(CDPATH= cd -- "$TARGET_DIR/.venv" && pwd)
  if [ "$VENV_DIR" != "$TARGET_DIR/.venv" ]; then
    printf '%s\n' "Refusing to remove an unexpected virtual-environment path." >&2
    exit 1
  fi
  rm -rf "$TARGET_DIR/.venv"
  printf '%s\n' "Removed virtual environment: $TARGET_DIR/.venv"
fi

if [ "$REMOVE_APP" -eq 1 ]; then
  if [ "$ASSUME_YES" -ne 1 ]; then
    printf '%s\n' "Refusing to remove application files without --yes." >&2
    exit 2
  fi
  if [ ! -f "$FILE_LIST" ] && [ -f "$SCRIPT_DIR/payload-files.txt" ]; then
    FILE_LIST="$SCRIPT_DIR/payload-files.txt"
  fi
  if [ ! -f "$FILE_LIST" ]; then
    printf '%s\n' "The exact installed payload file list was not found; nothing else was removed." >&2
    exit 1
  fi
  while IFS= read -r RELATIVE_PATH || [ -n "$RELATIVE_PATH" ]; do
    case "$RELATIVE_PATH" in
      ""|/*|../*|*/../*|*/..|*///*)
        printf '%s\n' "Unsafe path in installed payload list: $RELATIVE_PATH" >&2
        exit 1
        ;;
    esac
    rm -f "$TARGET_DIR/$RELATIVE_PATH"
  done < "$FILE_LIST"
  rm -f "$MARKER" "$TARGET_DIR/.pc-agent-posix-files.txt"
  for DIR in src scripts docs examples tests; do
    if [ -d "$TARGET_DIR/$DIR" ]; then
      find "$TARGET_DIR/$DIR" -depth -type d -empty -exec rmdir {} \; 2>/dev/null || true
    fi
  done
  printf '%s\n' "Removed exact published application payload files."
fi

printf '%s\n' "Preserved user .env, data, workspace files, and OS credentials."
printf '%s\n' "If no longer needed, inspect and manually remove: $TARGET_DIR"

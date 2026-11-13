#!/usr/bin/env bash
# casine installer — copies the files, checks dependencies.
# Usage:
#   ./install.sh              install (configs are never overwritten)
#   ./install.sh --force      overwrite existing configs too
#   ./install.sh uninstall    remove casine
#   PREFIX=/usr ./install.sh  install system-wide (needs write access)

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local/bin}"
CONF_DIR="$HOME/.config/casine"
FORCE=0
ACTION=install
[[ "${1:-}" == "--force" ]] && { FORCE=1; shift; }
[[ "${1:-}" == "uninstall" ]] && ACTION=uninstall

if [[ $ACTION == uninstall ]]; then
    rm -f "$PREFIX/casine"
    echo "removed $PREFIX/casine (configs kept in $CONF_DIR)"
    exit 0
fi

# --- files shipped next to this script --------------------------------------
for f in casine casine.ini cava.conf; do
    [[ -f "$SRC_DIR/$f" ]] || { echo "error: $SRC_DIR/$f not found" >&2; exit 1; }
done

# --- dependency checks -------------------------------------------------------
missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v cava     >/dev/null || missing+=("cava")
if command -v python3 >/dev/null && ! python3 -c "import numpy" 2>/dev/null; then
    missing+=("python-numpy")
fi
if ((${#missing[@]})); then
    echo "missing dependencies: ${missing[*]}" >&2
    echo "e.g. on Arch:  sudo pacman -S python python-numpy cava" >&2
    exit 1
fi

# --- install -----------------------------------------------------------------
mkdir -p "$PREFIX" "$CONF_DIR"
install -m 755 "$SRC_DIR/casine" "$PREFIX/casine"

if [[ ! -f "$CONF_DIR/cava.conf" || $FORCE -eq 1 ]]; then
    install -m 644 "$SRC_DIR/cava.conf" "$CONF_DIR/cava.conf"
    echo "installed  $CONF_DIR/cava.conf"
else
    echo "kept       $CONF_DIR/cava.conf (exists)"
fi

if [[ ! -f "$CONF_DIR/casine.ini" || $FORCE -eq 1 ]]; then
    install -m 644 "$SRC_DIR/casine.ini" "$CONF_DIR/casine.ini"
    echo "installed  $CONF_DIR/casine.ini"
else
    echo "kept       $CONF_DIR/casine.ini (exists)"
fi

echo "installed  $PREFIX/casine"

case ":$PATH:" in
    *":$PREFIX:"*) ;;
    *) echo "note: $PREFIX is not in your PATH — add it to your shell rc." ;;
esac
echo "done. run 'casine' inside kitty."

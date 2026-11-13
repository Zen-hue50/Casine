#!/usr/bin/env bash
# shellcheck shell=bash
# SPDX-License-Identifier: GPL-3.0
#
# casine installer - installs the "casine" command and its private configs.
#
#   what goes where:
#     casine.py  -> $PREFIX/casine         the command (default ~/.local/bin)
#     casine.ini -> ~/.config/casine/      your settings; never overwritten
#     cava.conf  -> ~/.config/casine/      unless you pass --force
#
# Usage:
#   ./install.sh                    install for the current user
#   ./install.sh --force            overwrite existing configs too
#   ./install.sh --prefix DIR       install into DIR (or: PREFIX=DIR ./install.sh)
#   ./install.sh uninstall          remove the casine command (configs are kept)
#   ./install.sh uninstall --purge  also delete the configs in ~/.config/casine
#
# Project: https://github.com/Zen-hue50/Casine

# this script needs bash; re-exec if launched with sh/dash
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

set -euo pipefail

: "${HOME:?error: HOME is not set - cannot determine install locations}"

have() { command -v "$1" >/dev/null 2>&1; }
die()  { echo "error: $*" >&2; exit 1; }
warn() { echo "warn:  $*" >&2; }

usage() {
    cat <<'USAGE'
casine installer

  ./install.sh                    install for the current user
  ./install.sh --force            overwrite existing configs too
  ./install.sh --prefix DIR       install into DIR (or: PREFIX=DIR ./install.sh)
  ./install.sh uninstall          remove the casine command (configs are kept)
  ./install.sh uninstall --purge  also delete the configs in ~/.config/casine

The command is installed as "casine" (built from casine.py next to this
script) and is meant to be run inside the kitty terminal.
USAGE
}

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local/bin}"
CONF_DIR="$HOME/.config/casine"
FORCE=0
PURGE=0
ACTION=install

while (($#)); do
    case "$1" in
        uninstall)  ACTION=uninstall ;;
        --force)    FORCE=1 ;;
        --purge)    PURGE=1 ;;
        --prefix)   if (($# < 2)); then die "--prefix needs a directory argument"; fi
                    PREFIX="$2"; shift ;;
        --prefix=*) PREFIX="${1#*=}" ;;
        -h|--help)  usage; exit 0 ;;
        *)          die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

if [[ -z "$PREFIX" ]]; then die "install prefix is empty"; fi
if ((PURGE)) && [[ "$ACTION" != "uninstall" ]]; then
    warn "--purge only has an effect together with 'uninstall'"
fi

# ------------------------------------------------------------- uninstall -----
if [[ "$ACTION" == "uninstall" ]]; then
    if [[ -e "$PREFIX/casine" || -L "$PREFIX/casine" ]]; then
        rm -f "$PREFIX/casine"
        echo "removed    $PREFIX/casine"
    else
        echo "nothing to remove at $PREFIX/casine"
    fi
    if ((PURGE)); then
        rm -rf "$CONF_DIR"
        echo "removed    $CONF_DIR"
    else
        if [[ -d "$CONF_DIR" ]]; then
            echo "configs kept in $CONF_DIR (pass --purge to delete them too)"
        else
            echo "no configs at $CONF_DIR"
        fi
    fi
    exit 0
fi

# ---------------------------------------------------------- source files -----
for f in casine.py casine.ini cava.conf; do
    if [[ ! -f "$SRC_DIR/$f" ]]; then
        die "$SRC_DIR/$f not found - run this script from the casine repo folder"
    fi
done

# --------------------------------------------------------- dependencies ------
missing=""
if ! have python3; then missing="$missing python3"; fi
if ! have cava;     then missing="$missing cava"; fi
if have python3 && ! python3 -c 'import numpy' >/dev/null 2>&1; then
    missing="$missing python-numpy"
fi

if [[ -n "$missing" ]]; then
    {
        echo "missing dependencies:${missing}"
        if have apt-get; then
            echo "e.g. Debian/Ubuntu:  sudo apt install python3 python3-numpy cava"
        elif have dnf; then
            echo "e.g. Fedora:         sudo dnf install python3 python3-numpy cava"
        elif have pacman; then
            echo "e.g. Arch:           sudo pacman -S python python-numpy cava"
        elif have zypper; then
            echo "e.g. openSUSE:       sudo zypper install python3 python3-numpy cava"
        elif have brew; then
            echo "e.g. macOS:          brew install python numpy cava"
        else
            echo "install python3, numpy and cava with your package manager"
        fi
    } >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' >/dev/null 2>&1; then
    die "python3 >= 3.6 is required (found: $(python3 -V 2>&1))"
fi

if ! have kitty; then
    warn "kitty is not in PATH - casine only runs inside the kitty terminal"
fi

if ((EUID == 0)) && [[ "$PREFIX" == "$HOME/.local/bin" ]]; then
    warn "installing as root into a home directory - did you mean: PREFIX=/usr/local/bin ./install.sh ?"
fi

# ------------------------------------------------- sanity-check casine.py ----
if ! python3 -c 'import sys; compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")' \
        "$SRC_DIR/casine.py" >/dev/null 2>&1; then
    die "$SRC_DIR/casine.py does not compile - fix it before installing"
fi

# ----------------------------------------------------------- destinations ----
if ! mkdir -p "$PREFIX" 2>/dev/null; then
    die "cannot create $PREFIX (no permission?) - try: PREFIX=\"$HOME/.local/bin\" ./install.sh"
fi
if [[ ! -w "$PREFIX" ]]; then
    die "$PREFIX is not writable - try: PREFIX=\"$HOME/.local/bin\" ./install.sh"
fi
if [[ -d "$PREFIX/casine" ]]; then
    die "$PREFIX/casine exists and is a directory - remove it first"
fi
if ! mkdir -p "$CONF_DIR" 2>/dev/null; then
    die "cannot create $CONF_DIR (no permission?)"
fi

# --------------------------------------------------------------- install -----
install -m 755 "$SRC_DIR/casine.py" "$PREFIX/casine"
echo "installed  $PREFIX/casine"

install_config() {
    local dst="$CONF_DIR/$1"
    if [[ -f "$dst" ]] && ((FORCE == 0)); then
        echo "kept       $dst (exists - rerun with --force to overwrite)"
    else
        install -m 644 "$SRC_DIR/$1" "$dst"
        echo "installed  $dst"
    fi
}
install_config cava.conf
install_config casine.ini

# the two raw-range values must agree, or the waves get scaled wrong
ini_max="$(awk -F'=' '/^[ \t]*raw_max[ \t]*=/{v=$2; sub(/[#;].*/, "", v); gsub(/[ \t\r]/, "", v); print v; exit}' "$CONF_DIR/casine.ini" 2>/dev/null || true)"
cava_max="$(awk -F'=' '/^[ \t]*ascii_max_range[ \t]*=/{v=$2; sub(/[#;].*/, "", v); gsub(/[ \t\r]/, "", v); print v; exit}' "$CONF_DIR/cava.conf" 2>/dev/null || true)"
if [[ -n "$ini_max" && -n "$cava_max" && "$ini_max" != "$cava_max" ]]; then
    warn "raw_max ($ini_max in casine.ini) differs from ascii_max_range ($cava_max in cava.conf) - waves will be scaled wrong"
fi

# ------------------------------------------------------------ smoke test -----
rc=0
env -u KITTY_WINDOW_ID TERM=xterm "$PREFIX/casine" </dev/null >/dev/null 2>&1 || rc=$?
if ((rc == 2)); then
    echo "checked    casine starts (python3 + numpy ok; it needs a real terminal to actually run)"
elif ((rc == 0)); then
    echo "checked    casine runs"
else
    warn "smoke test exited with status $rc - run 'casine' inside kitty to see the error"
fi

# ------------------------------------------------------------------ PATH -----
case ":$PATH:" in
    *":$PREFIX:"*) ;;
    *)
        warn "$PREFIX is not on your PATH - add it to your shell rc, e.g.:"
        echo "  echo 'export PATH=\"$PREFIX:\$PATH\"' >> ~/.bashrc" >&2
        ;;
esac

echo
echo "done. run it inside kitty:   casine"
echo "settings live in:            $CONF_DIR/casine.ini"
echo "uninstall:                   $(basename "$0") uninstall"

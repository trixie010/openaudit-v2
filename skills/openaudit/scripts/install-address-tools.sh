#!/usr/bin/env bash
# Install/check the address-audit toolchain without sudo.
#   cast, forge, anvil   -> Foundry (assumed installed)
#   evmole               -> Python wheel extracted into ~/.local/share/openaudit/pylibs
#   heimdall             -> prebuilt release into ~/.local/bin
#   ityfuzz              -> prebuilt nightly into ~/.local/bin (address-only fuzzing)
#
# Usage: install-address-tools.sh [--check] [--force] [--no-ityfuzz]
set -uo pipefail

CHECK_ONLY=0
FORCE=0
WITH_ITYFUZZ=1
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --force) FORCE=1 ;;
    --no-ityfuzz) WITH_ITYFUZZ=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

PYLIBS="${XDG_DATA_HOME:-$HOME/.local/share}/openaudit/pylibs"
BIN="$HOME/.local/bin"
mkdir -p "$PYLIBS" "$BIN"

missing=0
note() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

check_foundry() {
  local ok=1
  for b in cast forge anvil; do
    if have "$b"; then note "ok   $b  $("$b" --version 2>/dev/null | head -1)"; else note "MISS $b (install Foundry)"; ok=0; fi
  done
  [ "$ok" = 1 ]
}

check_evmole() {
  if PYTHONPATH="$PYLIBS" python3 -c "import evmole" >/dev/null 2>&1; then
    note "ok   evmole  $(PYTHONPATH="$PYLIBS" python3 -c 'import evmole; print(getattr(evmole,"__version__","installed"))' 2>/dev/null)"
    return 0
  fi
  note "MISS evmole ($PYLIBS)"
  return 1
}

check_heimdall() {
  local b
  b="$(command -v heimdall 2>/dev/null || true)"
  [ -z "$b" ] && [ -x "$BIN/heimdall" ] && b="$BIN/heimdall"
  if [ -n "$b" ]; then note "ok   heimdall  $("$b" --version 2>&1 | head -1)"; return 0; fi
  note "MISS heimdall ($BIN/heimdall)"
  return 1
}

check_ityfuzz() {
  local b
  b="$(command -v ityfuzz 2>/dev/null || true)"
  [ -z "$b" ] && [ -x "$BIN/ityfuzz" ] && b="$BIN/ityfuzz"
  if [ -n "$b" ]; then note "ok   ityfuzz  $("$b" --version 2>&1 | head -1)"; return 0; fi
  note "MISS ityfuzz ($BIN/ityfuzz)"
  return 1
}

install_evmole() {
  local tmp
  tmp="$(mktemp -d)"
  if ! pip download --no-deps --quiet -d "$tmp" evmole; then
    note "evmole install failed: pip download failed (need network + pip)"
    rm -r "$tmp" 2>/dev/null || true
    return 1
  fi
  python3 - "$tmp" "$PYLIBS" <<'PY'
import glob, os, sys, zipfile
tmp, dest = sys.argv[1], sys.argv[2]
wheels = glob.glob(os.path.join(tmp, "*.whl"))
if not wheels:
    raise SystemExit("no wheel downloaded")
with zipfile.ZipFile(wheels[0]) as z:
    z.extractall(dest)
print("evmole:", os.path.basename(wheels[0]))
PY
  rm -r "$tmp" 2>/dev/null || true
}

install_heimdall() {
  local ver arch url tmp
  ver="0.9.3"
  case "$(uname -m)" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) note "heimdall: unsupported arch $(uname -m)"; return 1 ;;
  esac
  url="https://github.com/Jon-Becker/heimdall-rs/releases/download/${ver}/heimdall-linux-${arch}"
  tmp="$(mktemp)"
  if ! curl -sSL --max-time 120 -o "$tmp" "$url"; then
    note "heimdall download failed"
    rm -f "$tmp"
    return 1
  fi
  install -m 755 "$tmp" "$BIN/heimdall"
  rm -f "$tmp"
}

install_ityfuzz() {
  local arch url tmp
  case "$(uname -m)" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) note "ityfuzz: unsupported arch $(uname -m)"; return 1 ;;
  esac
  url="https://github.com/fuzzland/ityfuzz/releases/download/nightly/ityfuzz_nightly_linux_${arch}.tar.gz"
  tmp="$(mktemp -d)"
  if ! curl -sSL --max-time 180 -o "$tmp/ityfuzz.tar.gz" "$url"; then
    note "ityfuzz download failed"
    rm -r "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! tar -xzf "$tmp/ityfuzz.tar.gz" -C "$tmp"; then
    note "ityfuzz extract failed"
    rm -r "$tmp" 2>/dev/null || true
    return 1
  fi
  install -m 755 "$tmp/ityfuzz" "$BIN/ityfuzz"
  rm -r "$tmp" 2>/dev/null || true
}

if [ "$CHECK_ONLY" = 1 ]; then
  check_foundry || missing=1
  check_evmole || missing=1
  check_heimdall || missing=1
  [ "$WITH_ITYFUZZ" = 1 ] && { check_ityfuzz || missing=1; }
  note ""
  note "pylibs: $PYLIBS"
  note "bin:    $BIN (ensure it is on PATH)"
  exit "$missing"
fi

check_foundry || missing=1

if ! check_evmole || [ "$FORCE" = 1 ]; then
  install_evmole || missing=1
  check_evmole || missing=1
fi

if ! check_heimdall || [ "$FORCE" = 1 ]; then
  install_heimdall || missing=1
  check_heimdall || missing=1
fi

if [ "$WITH_ITYFUZZ" = 1 ]; then
  if ! check_ityfuzz || [ "$FORCE" = 1 ]; then
    install_ityfuzz || missing=1
    check_ityfuzz || missing=1
  fi
fi

note ""
note "pylibs: $PYLIBS"
note "bin:    $BIN (ensure it is on PATH)"
exit "$missing"

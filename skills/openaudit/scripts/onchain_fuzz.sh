#!/usr/bin/env bash
# Bounty-mode onchain fuzzing against a deployed address, pinned to a block.
#
# Default engine is ItyFuzz:
#   - chains ItyFuzz knows: fork directly with -c <name> -b <block>
#   - any other EVM chain: anvil --fork-url <rpc> --fork-block-number <block>,
#     then ItyFuzz -c local (tested with Robinhood Chain 4663)
# Medusa is harness-only (it needs compiled artifacts); use --harness DIR with a
# medusa.json that forks and calls the deployed address.
#
# No transaction is broadcast: ItyFuzz and Medusa simulate on a local fork.
#
# Usage:
#   onchain_fuzz.sh --slug weth --chain 1 --target 0xC02a... --block 21000000 --timeout 300
#   onchain_fuzz.sh --slug rh --chain robinhood --target 0xTARGET --block 123456789
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
OUT=""
SLUG=""
CHAIN="1"
BLOCK_ARG=""
RPC_ARG=""
RPC_ENV_ARG=""
CHAINS_FILE="$ROOT/skills/openaudit/chains.json"
TIMEOUT=600
ENGINE="auto"
HARNESS=""
PORT=8545
FLASH=0
DRY=0
KEY="${ETHERSCAN_API_KEY:-}"
declare -a TARGETS_ARR=()

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,20p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --slug) SLUG="$2"; shift 2 ;;
    --chain) CHAIN="$2"; shift 2 ;;
    --target) IFS=',' read -ra parts <<< "$2"; TARGETS_ARR+=("${parts[@]}"); shift 2 ;;
    --block) BLOCK_ARG="$2"; shift 2 ;;
    --rpc-url) RPC_ARG="$2"; shift 2 ;;
    --rpc-env) RPC_ENV_ARG="$2"; shift 2 ;;
    --chains-file) CHAINS_FILE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --engine) ENGINE="$2"; shift 2 ;;
    --harness) HARNESS="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --flashloan) FLASH=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$SLUG" ] || die "--slug is required"
[ "${#TARGETS_ARR[@]}" -gt 0 ] || die "--target is required"
[ -f "$CHAINS_FILE" ] || die "chains file not found: $CHAINS_FILE"

if [ -f "$ROOT/env.sh" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/env.sh"
  set +a
fi

read -r CNAME RPC_ENV ITY_CHAIN PUBLIC_RPC < <(python3 - "$CHAIN" "$CHAINS_FILE" <<'PY'
import json, sys
chain, path = sys.argv[1], sys.argv[2]
data = json.load(open(path))
data = {k: v for k, v in data.items()
        if not str(k).startswith("_") and isinstance(v, dict)}
if chain in data:
    key = chain
else:
    key = next((k for k, v in data.items()
                if str(v.get("name", "")).lower() == chain.lower()), None)
    if key is None:
        raise SystemExit(f"unknown chain {chain}")
v = data[key]
print(v.get("name", ""), v.get("rpc_env", ""), v.get("ityfuzz") or "-",
      v.get("public_rpc") or "-")
PY
) || die "could not resolve chain $CHAIN"
[ -n "$ITY_CHAIN" ] || ITY_CHAIN="-"
[ -n "$PUBLIC_RPC" ] || PUBLIC_RPC="-"

RPC="${RPC_ARG:-}"
if [ -z "$RPC" ]; then
  ENV_NAME="${RPC_ENV_ARG:-$RPC_ENV}"
  RPC="$(printenv "$ENV_NAME" 2>/dev/null || true)"
fi
if [ -z "$RPC" ] && [ "$PUBLIC_RPC" != "-" ]; then RPC="$PUBLIC_RPC"; fi
[ -n "$RPC" ] || die "no RPC: set \$${RPC_ENV_ARG:-$RPC_ENV} or pass --rpc-url"

BLOCK="${BLOCK_ARG:-$(cast block-number --rpc-url "$RPC" 2>/dev/null)}"
[ -n "$BLOCK" ] || die "could not read block number"
TARGETS="$(IFS=,; echo "${TARGETS_ARR[*]}")"

OUT="${OUT:-$ROOT/out/$SLUG/fuzz}"
mkdir -p "$OUT"
CHAIN_ID="$(python3 - "$CHAIN" "$CHAINS_FILE" <<'PY'
import json, sys
chain, path = sys.argv[1], sys.argv[2]
data = json.load(open(path))
data = {k: v for k, v in data.items()
        if not str(k).startswith("_") and isinstance(v, dict)}
if chain in data:
    print(chain)
else:
    key = next((k for k, v in data.items()
                if str(v.get("name", "")).lower() == chain.lower()), None)
    print(key or "")
PY
)"

ITY_BIN="$(command -v ityfuzz 2>/dev/null || true)"
[ -z "$ITY_BIN" ] && [ -x "$HOME/.local/bin/ityfuzz" ] && ITY_BIN="$HOME/.local/bin/ityfuzz"
MEDUSA_BIN="$(command -v medusa 2>/dev/null || true)"

if [ "$ENGINE" = "auto" ]; then
  if [ -n "$ITY_BIN" ]; then ENGINE="ityfuzz"; elif [ -n "$MEDUSA_BIN" ] && [ -n "$HARNESS" ]; then ENGINE="medusa"; else ENGINE="ityfuzz"; fi
fi

ANVIL_PID=""
cleanup() { [ -n "$ANVIL_PID" ] && kill "$ANVIL_PID" 2>/dev/null || true; }
trap cleanup EXIT

write_readme() {
  cat > "$OUT/README.md" <<EOF
# Onchain fuzz - $SLUG

- Chain: \`$CHAIN_ID\` ($CNAME)
- Block: \`$BLOCK\`
- Targets: \`$TARGETS\`
- Engine: \`$ENGINE\`
- Mode: local fork only. Nothing is broadcast to mainnet/testnet.

Logs: \`run.log\` (and \`anvil.log\` when a local fork was used).
EOF
}

if [ "$DRY" = 1 ]; then
  echo "chain=$CNAME id=$CHAIN_ID block=$BLOCK targets=$TARGETS engine=$ENGINE"
  if [ "$ENGINE" = "ityfuzz" ]; then
    if [ "$ITY_CHAIN" = "-" ]; then
      echo "anvil --fork-url <rpc> --fork-block-number $BLOCK --port $PORT --silent"
      echo "$ITY_BIN evm -t $TARGETS -c local -b $BLOCK (rpc http://127.0.0.1:$PORT)${FLASH:+ [--flashloan]}"
    else
      echo "ETH_RPC_URL=<rpc> $ITY_BIN evm -t $TARGETS -c $ITY_CHAIN -b $BLOCK -k <key>"
    fi
  else
    echo "cd $HARNESS && medusa fuzz --config $OUT/medusa.json --no-color"
  fi
  exit 0
fi

write_readme

if [ "$ENGINE" = "ityfuzz" ]; then
  [ -n "$ITY_BIN" ] || die "ityfuzz not installed (run install-address-tools.sh)"
  if [ "$ITY_CHAIN" = "-" ]; then
    command -v anvil >/dev/null 2>&1 || die "anvil not installed (needed for custom chains)"
    if cast block-number --rpc-url "http://127.0.0.1:$PORT" >/dev/null 2>&1; then
      die "port $PORT already has an RPC; pass --port or stop it"
    fi
    anvil --fork-url "$RPC" --fork-block-number "$BLOCK" --port "$PORT" --silent \
      > "$OUT/anvil.log" 2>&1 &
    ANVIL_PID=$!
    ready=0
    for _ in $(seq 1 30); do
      if cast block-number --rpc-url "http://127.0.0.1:$PORT" >/dev/null 2>&1; then ready=1; break; fi
      sleep 1
    done
    [ "$ready" = 1 ] || die "anvil fork did not come up (see $OUT/anvil.log)"
    cmd=("$ITY_BIN" evm -t "$TARGETS" -c local -b "$BLOCK")
    export ETH_RPC_URL="http://127.0.0.1:$PORT"
  else
    cmd=("$ITY_BIN" evm -t "$TARGETS" -c "$ITY_CHAIN" -b "$BLOCK")
    export ETH_RPC_URL="$RPC"
  fi
  [ -n "$KEY" ] && cmd+=(-k "$KEY")
  [ "$FLASH" = 1 ] && cmd+=(--flashloan)
  echo "[fuzz] ${cmd[*]}" | sed "s#$RPC#<rpc>#g" | tee -a "$OUT/run.log"
  timeout "$TIMEOUT" "${cmd[@]}" 2>&1 | tee -a "$OUT/run.log"
  rc=${PIPESTATUS[0]}
  [ "$rc" = 124 ] && echo "[fuzz] timeout ${TIMEOUT}s (expected for a campaign)" | tee -a "$OUT/run.log"
  exit 0
fi

if [ "$ENGINE" = "medusa" ]; then
  [ -n "$MEDUSA_BIN" ] || die "medusa not installed"
  [ -n "$HARNESS" ] || die "medusa needs --harness DIR with a compiled fork harness"
  [ -f "$HARNESS/medusa.json" ] || die "missing $HARNESS/medusa.json"
  cmd=("$MEDUSA_BIN" fuzz --config "$HARNESS/medusa.json" --no-color)
  echo "[fuzz] cd $HARNESS && ${cmd[*]}" | tee -a "$OUT/run.log"
  ( cd "$HARNESS" && timeout "$TIMEOUT" "${cmd[@]}" ) 2>&1 | tee -a "$OUT/run.log"
  exit 0
fi

die "unknown engine: $ENGINE"

#!/usr/bin/env python3
"""Address-only intake: resolve proxies, recover source/ABI, and map privileged state.

Built for deployed targets where no repository is provided. Uses `cast` for chain
reads, Sourcify/Etherscan/Blockscout for verified source, and explorer or RPC logs
for role and upgrade history. Never prints or stores RPC URLs.

Chain support is config-driven via skills/openaudit/chains.json. Any EVM chain with
an RPC works; source/log backends degrade to decompilation when an explorer is absent.

Example:
  python3 skills/openaudit/scripts/address_intake.py \
    --slug invusd --chain 1 --block 26469201 \
    --address Lender=0xf8B349dA9244253288f6853835e6582955FD49c9
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CAST = shutil.which("cast") or "cast"
FORGE = shutil.which("forge") or "forge"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHAINS_FILE = ROOT / "skills" / "openaudit" / "chains.json"

DEFAULT_CHAINS = {
    "1": {
        "name": "ethereum", "rpc_env": "JSON_RPC_ETHEREUM", "sourcify": True,
        "etherscan": True, "blockscout": None, "rpc_logs": False,
        "forge_clone": "mainnet", "ityfuzz": "eth",
    },
    "4663": {
        "name": "robinhood", "rpc_env": "JSON_RPC_ROBINHOOD", "sourcify": False,
        "etherscan": False, "blockscout": "https://robinhoodchain.blockscout.com",
        "rpc_logs": True, "forge_clone": None, "ityfuzz": None,
    },
}

EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
EIP1967_BEACON = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
EIP1822_PROXIABLE = "0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7"
EIP1167_PREFIX = "0x363d3d373d3d3d363d73"
DIAMOND_FACETS_SELECTOR = "7a0ed627"

EVENT_SIGS = {
    "RoleGranted": "RoleGranted(bytes32,address,address)",
    "RoleRevoked": "RoleRevoked(bytes32,address,address)",
    "OwnershipTransferred": "OwnershipTransferred(address,address)",
    "Upgraded": "Upgraded(address)",
    "AdminChanged": "AdminChanged(address,address)",
    "BeaconUpgraded": "BeaconUpgraded(address)",
    "Paused": "Paused(address)",
    "Unpaused": "Unpaused(address)",
}

ROLE_NAMES = [
    "DEFAULT_ADMIN_ROLE", "ADMIN_ROLE", "MINTER_ROLE", "BURNER_ROLE", "PAUSER_ROLE",
    "UPGRADER_ROLE", "GOVERNOR_ROLE", "OPERATOR_ROLE", "MANAGER_ROLE", "GUARDIAN_ROLE",
    "RELAYER_ROLE", "SNAPSHOT_ROLE", "ORACLE_ROLE", "KEEPER_ROLE",
]

GETTERS = [
    ("owner", "owner()(address)"),
    ("pendingOwner", "pendingOwner()(address)"),
    ("admin", "admin()(address)"),
    ("operator", "operator()(address)"),
    ("manager", "manager()(address)"),
    ("guardian", "guardian()(address)"),
    ("authority", "authority()(address)"),
    ("proxyAdmin", "proxyAdmin()(address)"),
    ("implementation", "implementation()(address)"),
    ("delay", "delay()(uint256)"),
    ("minDelay", "getMinDelay()(uint256)"),
    ("MIN_DELAY", "MIN_DELAY()(uint256)"),
    ("threshold", "getThreshold()(uint256)"),
    ("owners", "getOwners()(address[])"),
    ("proxiableUUID", "proxiableUUID()(bytes32)"),
]


class RpcError(Exception):
    pass


def load_env_file(path: Path) -> None:
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val[:1] in ("'", '"') and val[-1:] == val[:1]:
            val = val[1:-1]
        os.environ.setdefault(key, val)


def load_env() -> None:
    for p in (Path.cwd() / "env.sh", ROOT / "env.sh", Path.home() / "env.sh"):
        if p.is_file():
            load_env_file(p)


def load_chains(path: Path) -> dict:
    data = None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = DEFAULT_CHAINS
    out = {}
    for key, val in data.items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        cfg = dict(val)
        cfg.setdefault("name", f"chain-{key}")
        cfg.setdefault(
            "rpc_env",
            "JSON_RPC_" + re.sub(r"[^A-Za-z0-9]+", "_", cfg["name"]).upper(),
        )
        cfg.setdefault("sourcify", False)
        cfg.setdefault("etherscan", False)
        cfg.setdefault("blockscout", None)
        cfg.setdefault("rpc_logs", False)
        cfg.setdefault("forge_clone", None)
        cfg.setdefault("ityfuzz", None)
        cfg.setdefault("public_rpc", None)
        out[str(key)] = cfg
    return out or {str(k): dict(v) for k, v in DEFAULT_CHAINS.items()}


def resolve_chain(value: str, chains: dict) -> str:
    if value in chains:
        return value
    for cid, cfg in chains.items():
        if value.lower() == str(cfg.get("name", "")).lower():
            return cid
    known = ", ".join(f"{cid}={cfg.get('name')}" for cid, cfg in chains.items())
    sys.exit(f"unknown chain {value!r}; known: {known}")


def rpc_url_for(chain: str, chains: dict, args: argparse.Namespace) -> tuple[str, str]:
    if args.rpc_url:
        return args.rpc_url, "custom"
    env_name = args.rpc_env or chains[chain]["rpc_env"]
    url = os.environ.get(env_name, "")
    if not url:
        url = chains[chain].get("public_rpc") or ""
        if url:
            return url, "public"
    if not url:
        sys.exit(f"missing RPC URL: set ${env_name} (or pass --rpc-url / --rpc-env)")
    return url, env_name


def cast_plain(*args: str, timeout: int = 60) -> str | None:
    try:
        p = subprocess.run([CAST, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


class Rpc:
    def __init__(self, url: str, timeout: int = 60):
        self.url = url
        self.timeout = timeout

    def cast(self, *args: str, timeout: int | None = None, allow_fail: bool = False) -> str | None:
        try:
            p = subprocess.run(
                [CAST, *args, "--rpc-url", self.url],
                capture_output=True, text=True, timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired:
            if allow_fail:
                return None
            raise RpcError("rpc timeout")
        if p.returncode != 0:
            if allow_fail:
                return None
            err = (p.stderr or p.stdout or "rpc error").strip().replace(self.url, "<rpc>")
            raise RpcError(err[:400])
        return p.stdout.strip()

    def block_number(self) -> int:
        return int(self.cast("block-number") or "0")

    def code(self, addr: str, block: int) -> str:
        return self.cast("code", addr, "--block", str(block)) or "0x"

    def codehash(self, addr: str, block: int) -> str:
        return self.cast("codehash", addr, "--block", str(block), allow_fail=True) or ""

    def storage(self, addr: str, slot: str, block: int) -> str | None:
        return self.cast("storage", addr, slot, "--block", str(block), allow_fail=True)

    def call(self, to: str, sig: str, block: int, *args: str) -> str | None:
        return self.cast("call", to, sig, *args, "--block", str(block), allow_fail=True)


def checksum(addr: str) -> str:
    return cast_plain("to-check-sum-address", addr, timeout=15) or addr


def word_to_addr(word: str | None) -> str | None:
    if not word:
        return None
    h = word[2:] if word.startswith("0x") else word
    if not h or int(h, 16) == 0:
        return None
    return "0x" + h.rjust(64, "0")[-40:]


def http_json(url: str, tries: int = 3, timeout: int = 30):
    for attempt in range(tries):
        req = urllib.request.Request(
            url, headers={"User-Agent": "openaudit-intake/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt + 1 < tries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt + 1 < tries:
                time.sleep(1.0)
                continue
            return None
    return None


def etherscan(chain: str, params: dict, key: str, tries: int = 3):
    q = {k: v for k, v in params.items() if v is not None}
    q.update({"chainid": chain, "apikey": key})
    url = "https://api.etherscan.io/v2/api?" + urllib.parse.urlencode(q)
    for attempt in range(tries):
        data = http_json(url, tries=1)
        if not data:
            time.sleep(1.0 * (attempt + 1))
            continue
        if str(data.get("status")) == "1":
            return data.get("result")
        msg = str(data.get("result") or data.get("message") or "")
        if "rate limit" in msg.lower() or "too many" in msg.lower():
            time.sleep(2.0 * (attempt + 1))
            continue
        return None
    return None


def etherscan_logs(chain: str, address: str, topic0: str, block: int, key: str,
                   max_pages: int) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        res = etherscan(chain, {
            "module": "logs", "action": "getLogs", "address": address,
            "fromBlock": 0, "toBlock": block, "topic0": topic0,
            "page": page, "offset": 1000,
        }, key)
        if not isinstance(res, list) or not res:
            break
        out.extend(res)
        if len(res) < 1000:
            break
        time.sleep(0.25)
    return out


def rpc_logs(rpc: Rpc, address: str, topic0: str, block: int) -> list[dict] | None:
    raw = rpc.cast(
        "logs", "--json", "--from-block", "0", "--to-block", str(block),
        "--address", address, topic0, timeout=180, allow_fail=True,
    )
    if not raw:
        return None
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return rows if isinstance(rows, list) else None


def blockscout_logs(base: str, address: str, topic0: str, max_pages: int) -> list[dict] | None:
    base = base.rstrip("/")
    url = f"{base}/api/v2/addresses/{address}/logs"
    params: dict = {}
    out: list[dict] = []
    pages = min(200, max(1, max_pages * 5))
    for _ in range(pages):
        q = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        data = http_json(q, tries=2, timeout=60)
        if not isinstance(data, dict):
            return None if not out else out
        items = data.get("items") or []
        for item in items:
            topics = item.get("topics") or []
            if topics and str(topics[0]).lower() == topic0.lower():
                out.append({
                    "topics": topics,
                    "blockNumber": item.get("block_number"),
                    "transactionHash": item.get("transaction_hash"),
                })
        nxt = data.get("next_page_params")
        if not nxt:
            break
        params = dict(nxt)
        time.sleep(0.2)
    return out


def logs_backend(cfg: dict, key: str | None) -> str:
    if cfg.get("etherscan") and key:
        return "etherscan"
    if cfg.get("rpc_logs"):
        return "rpc"
    if cfg.get("blockscout"):
        return "blockscout"
    return "none"


def fetch_logs(backend: str, chain: str, cfg: dict, rpc: Rpc, address: str,
               topic0: str, block: int, key: str | None, max_pages: int) -> list[dict]:
    if backend == "etherscan" and key:
        return etherscan_logs(chain, address, topic0, block, key, max_pages)
    if backend == "rpc":
        return rpc_logs(rpc, address, topic0, block) or []
    if backend == "blockscout" and cfg.get("blockscout"):
        return blockscout_logs(cfg["blockscout"], address, topic0, max_pages) or []
    return []


def topic_addr(topic: str | None) -> str | None:
    return "0x" + topic[-40:] if topic else None


def topic_hash(topic: str | None) -> str | None:
    if not topic:
        return None
    h = topic[2:] if topic.startswith("0x") else topic
    return "0x" + h.rjust(64, "0")


def safe_rel(path: str) -> str:
    path = path.replace("\\", "/").lstrip("/")
    parts = [p for p in path.split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "source.sol"


def parse_etherscan_sources(raw: str, contract_name: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw or raw.lower().startswith("contract source code not verified"):
        return {}
    if raw.startswith("{{") and raw.endswith("}}"):
        raw = raw[1:-1]
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("sources"), dict):
            return {
                path: (val.get("content", "") if isinstance(val, dict) else str(val))
                for path, val in obj["sources"].items()
            }
    return {f"{contract_name or 'Contract'}.sol": raw}


def sourcify_fetch(chain: str, address: str):
    data = http_json(f"https://sourcify.dev/server/v2/contract/{chain}/{address}?fields=all")
    return data if isinstance(data, dict) and isinstance(data.get("sources"), dict) else None


def blockscout_source(base: str, address: str):
    base = base.rstrip("/")
    data = http_json(f"{base}/api/v2/smart-contracts/{address}", tries=2, timeout=20)
    if not isinstance(data, dict):
        return None
    name = data.get("name") or "Contract"
    files = parse_etherscan_sources(data.get("source_code") or "", name)
    for item in data.get("additional_sources") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("file_path") or item.get("name") or "source.sol"
        content = item.get("source_code") or item.get("content") or ""
        if content:
            files[path] = content
    if not files or not data.get("abi"):
        return None
    return {
        "kind": "blockscout", "contract_name": name,
        "compiler": data.get("compiler_version", ""), "abi": data.get("abi"),
        "files": files, "match": "verified", "proxy_resolution": {},
    }


def fetch_source(chain: str, cfg: dict, address: str, key: str | None):
    if cfg.get("sourcify"):
        data = sourcify_fetch(chain, address)
        if data:
            files = {
                path: (val.get("content", "") if isinstance(val, dict) else str(val))
                for path, val in data["sources"].items()
            }
            comp = data.get("compilation") or {}
            return {
                "kind": "sourcify", "contract_name": comp.get("name", ""),
                "compiler": comp.get("compilerVersion", ""), "abi": data.get("abi"),
                "files": files, "match": data.get("match", ""),
                "proxy_resolution": data.get("proxyResolution") or {},
            }
    if cfg.get("etherscan") and key:
        res = etherscan(chain, {
            "module": "contract", "action": "getsourcecode", "address": address,
        }, key)
        if isinstance(res, list) and res and isinstance(res[0], dict) and res[0].get("SourceCode"):
            row = res[0]
            name = row.get("ContractName", "")
            abi = None
            try:
                parsed = json.loads(row.get("ABI", ""))
                abi = parsed if isinstance(parsed, list) else None
            except (json.JSONDecodeError, TypeError):
                pass
            return {
                "kind": "etherscan", "contract_name": name,
                "compiler": row.get("CompilerVersion", ""), "abi": abi,
                "files": parse_etherscan_sources(row.get("SourceCode", ""), name),
                "match": "verified", "proxy_resolution": {},
                "etherscan_proxy": row.get("Proxy") == "1",
                "etherscan_implementation": row.get("Implementation") or None,
            }
    if cfg.get("blockscout"):
        data = blockscout_source(cfg["blockscout"], address)
        if data:
            return data
    return None


def write_sources(label: str, files: dict[str, str], src_root: Path) -> list[str]:
    written = []
    for path, content in files.items():
        dest = src_root / label / safe_rel(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        written.append(str(dest.relative_to(src_root.parent.parent)))
    return written


def resolve_proxy(rpc: Rpc, address: str, block: int) -> dict:
    info = {"kind": "contract", "implementation": None, "beacon": None,
            "admin": None, "proxy_type": None, "tags": []}
    code = rpc.code(address, block)
    if code in ("", "0x"):
        info["kind"] = "eoa"
        return info
    if code.startswith(EIP1167_PREFIX) and len(code) >= 62:
        info.update(kind="proxy", proxy_type="eip1167",
                    implementation=checksum("0x" + code[22:62]))
        return info

    impl = word_to_addr(rpc.storage(address, EIP1967_IMPL, block))
    admin = word_to_addr(rpc.storage(address, EIP1967_ADMIN, block))
    beacon = word_to_addr(rpc.storage(address, EIP1967_BEACON, block))
    if impl:
        info.update(kind="proxy", proxy_type="eip1967", implementation=checksum(impl),
                    admin=checksum(admin) if admin else None)
    elif beacon:
        beacon_impl = rpc.call(beacon, "implementation()(address)", block)
        info.update(kind="proxy", proxy_type="eip1967-beacon", beacon=checksum(beacon),
                    implementation=checksum(beacon_impl) if beacon_impl else None,
                    admin=checksum(admin) if admin else None)
    else:
        proxiable = word_to_addr(rpc.storage(address, EIP1822_PROXIABLE, block))
        if proxiable:
            info.update(kind="proxy", proxy_type="eip1822",
                        implementation=checksum(proxiable))

    sel_out = rpc.cast("disassemble", "--color", "never", code, allow_fail=True)
    if sel_out and DIAMOND_FACETS_SELECTOR in sel_out:
        info["tags"].append("diamond")
        facets = rpc.call(address, "facetAddresses()(address[])", block)
        if facets:
            info["facets"] = [f.strip() for f in re.findall(r"0x[0-9a-fA-F]{40}", facets)]

    threshold = rpc.call(address, "getThreshold()(uint256)", block)
    owners = rpc.call(address, "getOwners()(address[])", block)
    if threshold is not None and owners is not None:
        info["tags"].append("safe")
        info["safe_threshold"] = threshold
        info["safe_owners"] = re.findall(r"0x[0-9a-fA-F]{40}", owners)

    delay = rpc.call(address, "getMinDelay()(uint256)", block)
    if delay is None:
        delay = rpc.call(address, "delay()(uint256)", block)
    if delay is not None:
        info["tags"].append("timelock")
        info["delay"] = delay
    return info


def read_getters(rpc: Rpc, address: str, block: int) -> dict:
    values = {}
    for name, sig in GETTERS:
        out = rpc.call(address, sig, block)
        if out is None or out == "":
            continue
        if name == "owners":
            addrs = re.findall(r"0x[0-9a-fA-F]{40}", out)
            if addrs:
                values[name] = addrs
        elif name == "proxiableUUID":
            values[name] = out
        elif re.fullmatch(r"0x[0-9a-fA-F]{40}", out):
            values[name] = checksum(out)
        else:
            values[name] = out
    return values


def collect_roles(rpc: Rpc, backend: str, chain: str, cfg: dict, address: str, block: int,
                  key: str | None, max_pages: int, verify: bool) -> dict:
    if backend == "none":
        return {}
    topic0 = {k: cast_plain("keccak", sig) for k, sig in EVENT_SIGS.items()}
    roles: dict[str, dict] = {}
    for event in ("RoleGranted", "RoleRevoked"):
        for log in fetch_logs(backend, chain, cfg, rpc, address, topic0[event] or "",
                              block, key, max_pages):
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            role = topic_hash(topics[1])
            account = topic_addr(topics[2])
            if not role or not account:
                continue
            entry = roles.setdefault(role, {"holders": set(), "grants": 0, "revokes": 0})
            if event == "RoleGranted":
                entry["holders"].add(checksum(account))
                entry["grants"] += 1
            else:
                entry["holders"].discard(checksum(account))
                entry["revokes"] += 1
    labels = {cast_plain("keccak", n): n for n in ROLE_NAMES}
    labels["0x" + "0" * 64] = "DEFAULT_ADMIN_ROLE"
    out = {}
    for role, entry in roles.items():
        holders = sorted(entry["holders"])
        verified = {}
        if verify:
            for holder in holders[:50]:
                res = rpc.call(address, "hasRole(bytes32,address)(bool)", block, role, holder)
                if res is not None:
                    verified[holder] = res
        out[role] = {
            "label": labels.get(role, ""), "holders": holders,
            "grants": entry["grants"], "revokes": entry["revokes"], "verified": verified,
        }
    return out


def collect_history(rpc: Rpc, backend: str, chain: str, cfg: dict, address: str, block: int,
                    key: str | None, max_pages: int) -> dict:
    if backend == "none":
        return {}
    topic0 = {k: cast_plain("keccak", sig) for k, sig in EVENT_SIGS.items()}
    out: dict[str, list] = {}
    for event in ("Upgraded", "AdminChanged", "BeaconUpgraded", "OwnershipTransferred",
                  "Paused", "Unpaused"):
        logs = fetch_logs(backend, chain, cfg, rpc, address, topic0[event] or "",
                          block, key, max_pages)
        if not logs:
            continue
        rows = []
        for log in logs:
            topics = log.get("topics") or []
            raw_block = str(log.get("blockNumber", "0"))
            try:
                block_no = int(raw_block, 16) if raw_block.startswith("0x") else int(raw_block)
            except ValueError:
                block_no = 0
            row = {"block": block_no, "tx": log.get("transactionHash", "")}
            if event == "Upgraded":
                row["implementation"] = topic_addr(topics[1] if len(topics) > 1 else None)
            elif event == "BeaconUpgraded":
                row["beacon"] = topic_addr(topics[1] if len(topics) > 1 else None)
            elif event == "AdminChanged":
                row["previous"] = topic_addr(topics[1] if len(topics) > 1 else None)
                row["new"] = topic_addr(topics[2] if len(topics) > 2 else None)
            elif event == "OwnershipTransferred":
                row["previous"] = topic_addr(topics[1] if len(topics) > 1 else None)
                row["new"] = topic_addr(topics[2] if len(topics) > 2 else None)
            elif event in ("Paused", "Unpaused"):
                row["account"] = topic_addr(topics[1] if len(topics) > 1 else None)
            rows.append(row)
        out[event] = sorted(rows, key=lambda r: r["block"])
    return out


def forge_clone(chain_name: str | None, address: str, dest: Path, key: str | None) -> str | None:
    if not chain_name or dest.exists():
        return None
    env = os.environ.copy()
    if key:
        env.setdefault("ETHERSCAN_API_KEY", key)
    try:
        p = subprocess.run(
            [FORGE, "clone", "--chain", chain_name, address, str(dest)],
            capture_output=True, text=True, timeout=300, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return str(dest) if p.returncode == 0 else None


def parse_address_specs(specs: list[str]) -> list[tuple[str, str]]:
    out = []
    seen = {}
    for raw in specs:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            name, _, addr = item.partition("=") if "=" in item else ("", "", item)
            if not addr:
                addr = name
                name = ""
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
                sys.exit(f"invalid address: {addr!r}")
            if addr.lower() in seen:
                continue
            seen[addr.lower()] = True
            out.append((name or addr[:10], checksum(addr)))
    return out


def write_scope(out_dir: Path, slug: str, chain: str, cfg: dict, block: int,
                addresses: list[tuple[str, str]]) -> None:
    path = out_dir / "SCOPE.md"
    if path.exists():
        return
    lines = [
        f"# Scope: {slug}", "",
        f"- Chain: {chain} ({cfg['name']})",
        f"- Snapshot block: {block}",
        f"- Testing: local fork of {cfg['name']} pinned at the snapshot block; call deployed addresses.",
        "- No redeploy with mocks. No mainnet broadcast.", "",
        "## In-scope addresses", "",
    ]
    lines += [f"- `{name}`: `{addr}`" for name, addr in addresses]
    lines += ["", "## Source", "",
              "Recovered by `skills/openaudit/scripts/address_intake.py` into `src/`;",
              "verify the bytecode before trusting it (`forge verify-bytecode`).", ""]
    path.write_text("\n".join(lines))


def write_report(out_dir: Path, slug: str, chain: str, cfg: dict, block: int,
                 rpc_env: str, facts: dict) -> Path:
    path = out_dir / "reports" / f"addresses-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Address intake - {slug}", "",
        f"- Chain: `{chain}` ({cfg['name']})",
        f"- Block: `{block}`",
        f"- RPC env: `{rpc_env}` (value never stored)",
        f"- Generated: {facts['generated_at']}", "",
        "## Contracts", "",
        "| name | address | kind | impl / beacon | verified | codehash |",
        "|---|---|---|---|---|---|",
    ]
    for row in facts["addresses"]:
        impl = row.get("implementation") or row.get("beacon") or "-"
        codehash = row.get("codehash") or "-"
        if len(codehash) > 18:
            codehash = codehash[:10] + "..." + codehash[-6:]
        kind = (row.get("kind") or "unknown")
        kind += f"/{row['proxy_type']}" if row.get("proxy_type") else ""
        lines.append(
            f"| {row['name']} | `{row['address']}` | {kind} | `{impl}`"
            f" | {row.get('source_kind') or 'none'} | `{codehash}` |"
        )

    lines += ["", "## Privileged state", ""]
    any_state = False
    for row in facts["addresses"]:
        values = row.get("getters") or {}
        if not values:
            continue
        any_state = True
        lines += [f"### {row['name']} (`{row['address']}`)", ""]
        for k, v in values.items():
            if isinstance(v, list):
                v = ", ".join(f"`{x}`" for x in v)
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    if not any_state:
        lines += ["_No privileged getters returned data._", ""]

    lines += ["## Roles", ""]
    any_roles = False
    for row in facts["addresses"]:
        roles = row.get("roles") or {}
        if not roles:
            continue
        any_roles = True
        lines += [f"### {row['name']} (`{row['address']}`)", "",
                  "| role | hash | holders | grants / revokes |",
                  "|---|---|---|---|"]
        for role, info in roles.items():
            label = info.get("label") or "unknown"
            holders = ", ".join(f"`{h}`" for h in info.get("holders", [])) or "-"
            lines.append(
                f"| {label} | `{role}` | {holders} | {info.get('grants', 0)} / {info.get('revokes', 0)} |"
            )
        lines.append("")
    if not any_roles:
        lines += ["_No role events found (or logs skipped)._", ""]

    lines += ["## Upgrade / admin history", ""]
    any_hist = False
    for row in facts["addresses"]:
        hist = row.get("history") or {}
        if not hist:
            continue
        any_hist = True
        lines += [f"### {row['name']} (`{row['address']}`)", "",
                  "| event | block | tx | value |", "|---|---|---|---|"]
        for event, rows in hist.items():
            for r in rows:
                value = (r.get("implementation") or r.get("beacon") or r.get("new")
                         or r.get("account") or "")
                lines.append(
                    f"| {event} | {r.get('block')} | `{str(r.get('tx', ''))[:12]}...` | `{value}` |"
                )
        lines.append("")
    if not any_hist:
        lines += ["_No upgrade/admin events found (or logs skipped)._", ""]

    lines += ["## Source recovery", ""]
    for row in facts["addresses"]:
        files = row.get("source_files") or []
        if files:
            lines.append(
                f"- `{row['name']}` ({row.get('source_kind')}, `{row.get('contract_name')}`): "
                f"{len(files)} file(s)"
            )
        else:
            lines.append(
                f"- `{row['name']}`: **no verified source** - decompile with heimdall/evmole"
            )
    if facts.get("errors"):
        lines += ["", "## Errors", ""] + [f"- {e}" for e in facts["errors"]]
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--slug", required=True)
    ap.add_argument("--chain", default="1")
    ap.add_argument("--address", action="append", required=True,
                    help="ADDRESS or NAME=ADDRESS; repeatable (comma-separated also accepted)")
    ap.add_argument("--block", default="latest")
    ap.add_argument("--out", default=None)
    ap.add_argument("--rpc-url", default=None)
    ap.add_argument("--rpc-env", default=None)
    ap.add_argument("--etherscan-key", default=None)
    ap.add_argument("--chains-file", default=str(DEFAULT_CHAINS_FILE))
    ap.add_argument("--skip-logs", action="store_true")
    ap.add_argument("--skip-source", action="store_true")
    ap.add_argument("--forge-clone", action="store_true",
                    help="also run `forge clone` per address (slow, needs explorer key)")
    ap.add_argument("--max-log-pages", type=int, default=20)
    ap.add_argument("--no-verify-roles", action="store_true")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--json", action="store_true", help="print facts JSON to stdout")
    args = ap.parse_args()

    load_env()
    chains = load_chains(Path(args.chains_file))
    chain = resolve_chain(args.chain, chains)
    cfg = chains[chain]
    key = (args.etherscan_key or os.environ.get("ETHERSCAN_API_KEY")
           or os.environ.get("ETHERSCAN_KEY"))
    url, rpc_env = rpc_url_for(chain, chains, args)
    rpc = Rpc(url, timeout=args.timeout)

    actual_chain = rpc.cast("chain-id")
    if actual_chain != chain:
        sys.exit(f"RPC chain id {actual_chain} does not match --chain {chain}")

    head = rpc.block_number()
    block = head if args.block == "latest" else int(args.block)
    if block > head:
        sys.exit(f"block {block} is ahead of chain head {head}")

    out_dir = Path(args.out) if args.out else ROOT / "out" / args.slug
    src_root = out_dir / "src"
    abi_dir = out_dir / "abi"
    for d in (src_root, abi_dir, out_dir / "_meta", out_dir / "reports"):
        d.mkdir(parents=True, exist_ok=True)

    addresses = parse_address_specs(args.address)
    backend = "none" if args.skip_logs else logs_backend(cfg, key)
    facts = {
        "slug": args.slug, "chain": chain, "chain_name": cfg["name"],
        "block": block, "rpc_env": rpc_env, "logs_backend": backend,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "addresses": [], "errors": [],
    }
    all_files: list[str] = []
    fetched: set[str] = set()

    for name, addr in addresses:
        print(f"[intake] {name} {addr}", file=sys.stderr)
        row = {"name": name, "address": addr, "logs_via": backend, "kind": "unknown"}
        try:
            row["codehash"] = rpc.codehash(addr, block)
            row.update(resolve_proxy(rpc, addr, block))
            row["getters"] = read_getters(rpc, addr, block)
            if backend != "none":
                row["roles"] = collect_roles(
                    rpc, backend, chain, cfg, addr, block, key,
                    args.max_log_pages, not args.no_verify_roles,
                )
                row["history"] = collect_history(
                    rpc, backend, chain, cfg, addr, block, key, args.max_log_pages
                )
            if not args.skip_source:
                src = fetch_source(chain, cfg, addr, key)
                if src:
                    files = write_sources(name, src["files"], src_root)
                    row.update(
                        source_kind=src["kind"], contract_name=src["contract_name"],
                        compiler=src["compiler"], source_files=files,
                    )
                    if src.get("match"):
                        row["source_match"] = src["match"]
                    if src.get("abi"):
                        abi_path = abi_dir / f"{name}.json"
                        abi_path.write_text(json.dumps(src["abi"], indent=2))
                        row["abi"] = str(abi_path.relative_to(out_dir))
                    all_files.extend(files)
                    fetched.add(addr.lower())
                else:
                    row.update(source_kind="none", source_files=[])

                impl = row.get("implementation")
                if impl and impl.lower() not in fetched:
                    isrc = fetch_source(chain, cfg, impl, key)
                    if isrc:
                        label = f"{name}__impl"
                        files = write_sources(label, isrc["files"], src_root)
                        row["impl_source_files"] = files
                        row["impl_source_kind"] = isrc["kind"]
                        row["impl_contract_name"] = isrc["contract_name"]
                        if isrc.get("abi"):
                            (abi_dir / f"{label}.json").write_text(
                                json.dumps(isrc["abi"], indent=2)
                            )
                        all_files.extend(files)
                        fetched.add(impl.lower())

                if args.forge_clone:
                    dest = out_dir / "clone" / name
                    cloned = forge_clone(cfg.get("forge_clone"), impl or addr, dest, key)
                    if cloned:
                        row["forge_clone"] = cloned
                    else:
                        facts["errors"].append(f"forge clone unavailable/failed for {name}")
        except RpcError as exc:
            facts["errors"].append(f"{name}: {exc}")
            row["error"] = str(exc)
        except Exception as exc:  # one bad address should not kill intake
            facts["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            row["error"] = f"{type(exc).__name__}: {exc}"
        facts["addresses"].append(row)

    (out_dir / "reports" / "onchain-facts.json").write_text(
        json.dumps(facts, indent=2, default=str)
    )
    if all_files:
        (out_dir / "in-scope-files.txt").write_text("\n".join(sorted(set(all_files))) + "\n")
    write_scope(out_dir, args.slug, chain, cfg, block, addresses)
    report = write_report(out_dir, args.slug, chain, cfg, block, rpc_env, facts)

    print(f"[intake] report: {report}", file=sys.stderr)
    print(f"[intake] facts:  {out_dir / 'reports' / 'onchain-facts.json'}", file=sys.stderr)
    if facts["errors"]:
        print(f"[intake] {len(facts['errors'])} warning(s); see report", file=sys.stderr)
    if args.json:
        print(json.dumps(facts, indent=2, default=str))
    return 0 if any("error" not in r for r in facts["addresses"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

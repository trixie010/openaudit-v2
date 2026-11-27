# Address-only audit playbook (deployed contracts, no repo)

Use when the target is one or more deployed addresses and the program expects testing on a local
mainnet fork (or, only when explicitly allowed, a live testnet). The source repo is not given.

The failure this prevents: writing `forge test` PoCs that deploy a fresh copy with mocks. That
tests *your reconstruction*, not the deployed system. Every claim about a live contract must be
reproduced against the deployed address on a fork pinned to a block.

## 0. Non-negotiables

1. **Fork, do not redeploy.** In-scope addresses are called on a fork; the target contract is never
   re-deployed in the PoC. Local deploys are only for targets with no deployment.
2. **Pin the block.** `vm.createSelectFork(rpc, BLOCK)` or `--fork-url ... --fork-block-number N`.
   Record the block in `SCOPE.md`, the PoC header, and the submission.
3. **Assert the fork is real.** Every PoC inherits `templates/fork/ForkBase.t.sol`, which fails
   unless a fork is active, the chain id and block match, and the target has code.
4. **No mainnet broadcast.** `anvil`/`forge test` against a fork is local simulation. Never send a
   transaction to mainnet or a public testnet. Testnet use is allowed only when the program says so,
   with accounts/assets you own, and never as the sole evidence if the bug exists on mainnet.
5. **Impact is a balance/invariant delta**, not "the call reverts" or a logged number. Assert
   attacker gain or protocol loss before/after.

## 1. Intake (before any analysis)

Collect: chain id, address(es), snapshot block, RPC, explorer, program scope wording.

```bash
cast chain-id --rpc-url "$RPC"
cast block-number --rpc-url "$RPC"
cast code 0xADDR --rpc-url "$RPC" > /tmp/target.hex     # empty => EOA or wrong chain
cast codehash 0xADDR --rpc-url "$RPC"                   # pin this in the report
```

Record in `out/{slug}/SCOPE.md`: chain, block, each address, role (proxy/impl/token/feed), and
whether it is verified. Snapshot block must be >= the deployment block and >= the last upgrade you
intend to test.

## 2. Recover source (in this order)

1. **`forge clone`** — best: full Foundry project + compiler settings + `.clone.meta` (creation tx,
   deployer, constructor args, storage layout).
   ```bash
   forge clone --chain mainnet --implementation 0xADDR out/{slug}/clone
   ```
   `--implementation` resolves the proxy via Etherscan metadata (Etherscan only; not with Sourcify).
2. **Sourcify** (no key, exact/partial match):
   `curl "https://sourcify.dev/server/files/any/1/0xADDR"`
3. **Etherscan API v2** (`chainid=1&module=contract&action=getsourcecode`) — broadest coverage.
4. **Blockscout** (`https://eth.blockscout.com/api?module=contract&action=getsourcecode`) fallback.
5. **No verified source** — decompile and recover the interface (section 3), then test behavior on
   the fork. Label the evidence boundary `decompiled`, never `source`.

**Always check source == deployed bytecode** before trusting it:

```bash
forge verify-bytecode 0xADDR src/Contract.sol:Contract --block "$BLOCK" --rpc-url "$RPC"
# or compare runtime bytecode hash from cast codehash against the compiled artifact
```

A verified source that does not match the runtime bytecode (wrong settings, modified metadata,
malicious fork) is not ground truth.

## 3. No source: recover the surface, then test behavior

```bash
cast code 0xADDR --rpc-url "$RPC" > target.hex
# evmole: selectors, arg types, mutability, storage layout (fast, scriptable)
evmole --bytecode target.hex
# heimdall: disassembly, CFG, storage dump, decompile
heimdall disassemble target.hex -o target.asm
heimdall cfg target.hex -o target.dot
heimdall storage-dump 0xADDR --rpc-url "$RPC" -o storage.json
```

Then: diff the selector set against any known ABI; find selectors that guard on `CALLER` vs a
storage slot; check `DELEGATECALL`/`SELFDESTRUCT` reachability; decode real calldata with
`cast tx`/`heimdall decode-calldata`. Decompilers guess names — verify every claim against the
disassembly, a storage read, or a fork run.

## 4. Proxy and system discovery

Check these slots with `cast storage 0xADDR <slot>`:

| Pattern | Slot / method |
|---|---|
| EIP-1967 implementation | `0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc` |
| EIP-1967 admin | `0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103` |
| EIP-1967 beacon | `0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50` |
| EIP-1822 PROXIABLE | `0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7` |
| EIP-1167 minimal proxy | runtime bytecode prefix `363d3d373d3d3d363d73...5af43d82803e903d91602b57fd5bf3` |
| EIP-2535 diamond | call `facets()((address,bytes4[])[])` and fetch each facet |
| Beacon | call `implementation()` on the beacon address |

Also enumerate: `owner()`, `pendingOwner()`, `admin()`, `operator()`, `manager()`, `guardian()`,
`getRoleMemberCount`/`getRoleMember`, `hasRole`; reconstruct role holders from
`RoleGranted`/`RoleRevoked` logs; list `Upgraded`/`AdminChanged`/`BeaconUpgraded` history; classify
EOA vs Safe (threshold/owners) vs timelock (`delay()`, `MIN_DELAY`). Save to
`reports/addresses-{slug}.md` and `reports/onchain-facts.json`.

Pin the implementation you audited. An implementation upgraded after your block is a different
target; say which one the PoC exercises.

## 5. Fork harness

`templates/fork/foundry.toml` + `templates/fork/ForkBase.t.sol`:

```solidity
contract Exploit is ForkBase {
    address constant LENDER = 0x...;

    function setUp() public {
        _selectFork("JSON_RPC_ETHEREUM", 1, 26_472_845); // chain id, pinned block
        _assertDeployed(LENDER);
        _assertProxyImpl(LENDER, 0x...);                  // if proxy
    }

    function test_exploit() public {
        uint256 before = IERC20(COIN).balanceOf(attacker);
        vm.prank(attacker);
        ILender(LENDER).liquidate(...);
        assertGt(IERC20(COIN).balanceOf(attacker), before);
    }
}
```

Rules:
- Fund actors with `deal(token, actor, amount)` or `vm.prank(whale)`; prefer a real whale over a
  mock. Mocking an oracle/feed is allowed only when the finding is about a state the chain can
  actually reach — disclose it in `evidence_boundary`.
- Use `vm.warp`/`vm.roll` deliberately; do not warp past a Chainlink heartbeat and then claim
  staleness without measuring how often that happens on-chain.
- Keep the PoC one file + `forge-std` where possible, so a triager can drop it into a fresh project.
- Run: `forge test --match-path test/poc/{id}.t.sol -vvv` with `JSON_RPC_ETHEREUM` set.

## 6. Live testnet mode (only if the program allows it)

Default is **local fork only**. Programs differ: some explicitly exclude testnets (findings must
reproduce on a mainnet fork), some allow a local fork of the testnet, a few allow interacting with a
live testnet. Before using it, quote the program rule in `SCOPE.md`.

If allowed: use only accounts/assets you own, never disrupt other users, snapshot before acting,
and treat the testnet as supporting evidence — reproduce the finding on a local mainnet fork if the
mainnet deployment is in scope. Testnet oracles, liquidity, roles and block times differ; do not
infer mainnet behavior from a testnet run.

## 7. Evidence bar for the review gate

A deployed-target finding is submission-grade only when:

- `evidence_boundary` starts with `fork:` (e.g. `fork:mainnet@26472845`), and
- the PoC asserts a balance/invariant delta on the deployed address, and
- the fork block and chain are recorded in the report, and
- no privileged `vm.prank` is the attacker (unless the finding is explicitly about a privileged
  path being reachable by an unprivileged actor).

Run `review_gate.py --require-fork` for address targets; it parks anything without fork evidence
at `high-risk`.

## 8. Common pitfalls

- Unpinned fork: state drifts, results are not reproducible.
- Redeploying the target with mocks: tests your copy, not production (this was the invusd PS-01 bug).
- Reading the proxy's storage layout instead of the implementation's.
- Trusting verified source without a bytecode check.
- Warping time to force oracle staleness/rebasing and reporting it as a production condition.
- Assuming testnet behavior equals mainnet (oracles, liquidity, roles, gas, L2 precompiles).
- Treating a decompiler's guessed names as source truth.

## Sources

- Foundry fork testing: https://www.getfoundry.sh/guides/fork-testing
- `vm.createSelectFork`: https://www.getfoundry.sh/reference/cheatcodes/create-select-fork
- `forge clone`: https://www.getfoundry.sh/forge/cloning
- `forge verify-bytecode`: https://www.getfoundry.sh/reference/forge/verify-bytecode
- Storage/upgrade rules: https://www.getfoundry.sh/guides/upgrading-contracts
- Sourcify: https://sourcify.dev/server/ ; Etherscan v2: https://docs.etherscan.io/
- evmole: https://github.com/cdump/evmole ; heimdall-rs: https://github.com/Jon-Becker/heimdall-rs
- ItyFuzz onchain mode: https://docs.ityfuzz.rs/quickstart
- Medusa fork config: https://book.getrecon.xyz/
- EIP-1967 / EIP-1822 / EIP-1167 / EIP-2535 specifications

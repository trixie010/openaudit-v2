---
name: vigilo_poc
description: >
  Generates and validates Foundry PoC tests (.t.sol) from audit findings/attack scenarios.
  Use when: writing proof-of-concept exploit tests for smart contract findings, converting
  an attack scenario into an executable Foundry test, validating a vulnerability with
  forge test, or building a PoC for a Medium/High/Critical finding.
  Outputs: test/poc/{finding-id}.t.sol and validation logs.
argument-hint: <finding-path.md>
---

# PoC Generation & Validation

**Input**: $ARGUMENTS (attack scenario file path)

---

# Part 1: PoC Code Generation

## Core Principle

```
1 Attack Scenario = 1 PoC Code
```

## Workflow

### 1. Read Attack Scenario

```
Read(".vigilo/findings/{severity}/{auditor}/{Severity}-{id}-{title}.md")
```

Example: `.vigilo/findings/high/logic/H-01-donation-attack-inflated-collateral.md`

Extract: Finding ID, Bug Class, Preconditions, Attack Steps, Impact, Vulnerable Code

### 2. Generate PoC

**File**: `test/poc/{Severity}-{id}-{title}.t.sol`

Example: `test/poc/H-01-donation-attack-inflated-collateral.t.sol`

**NOTE**: PoC code goes in `test/poc/` (project root), NOT in `.vigilo/` folder.

**Deployed target? Fork-first (mandatory).** If the finding concerns a live deployment, the PoC must
call the deployed address on a fork pinned to a block - never redeploy the target with mocks. Inherit
`skills/openaudit/templates/fork/ForkBase.t.sol` or start `setUp()` with
`vm.createSelectFork(vm.envString("JSON_RPC_ETHEREUM"), <block>)` and assert the fork, chain id,
block and target code before the attack. A local deploy is valid only when the target has no
deployment. Recover addresses/source with `skills/openaudit/scripts/address_intake.py` and diff
bytecode selectors with `scripts/selector_diff.py`. Full procedure: `skills/openaudit/address-audit.md`.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import "forge-std/console2.sol";

contract PoC_{ID}_{BugClass} is Test {
    // State variables, actors

    function setUp() public {
        // Reproduce preconditions
    }

    function test_Exploit_{ID}() public {
        // 1. Log initial state
        // 2. Execute attack steps
        // 3. Log final state
        // 4. Assert impact
    }
}
```

### 3. PoC Checklist

- [ ] setUp(): Fully reproduce preconditions
- [ ] Deployed target: pinned fork, deployed address, no redeploy and no mock-only evidence
- [ ] Actor labels: Use `makeAddr()` for all addresses
- [ ] console2.log: Output before/after attack state
- [ ] Attack steps: Follow scenario order exactly
- [ ] **Impact assertion: Directly prove scenario's claimed impact** (required)

### 4. Bug Class Patterns

Detailed templates: [references/poc-templates.md](references/poc-templates.md)

---

# Part 2: Foundry Validation

## Core Principle

```
Test Pass ≠ Vulnerability Validated
```

**Goal**: Verify that the Attack Scenario's claimed Impact actually occurs

## Workflow

### 1. Execute Test

```bash
forge test --match-test "test_Exploit_{ID}" -vvv
```

| Verbosity | Use Case |
|-----------|----------|
| `-vvv` | Default recommended |
| `-vvvvv` | Detailed debugging |

### 2. Analyze Results

| Result | Meaning | Action |
|--------|---------|--------|
| PASS + Impact proven | Vulnerability valid | → Attempt §6 differential (submission-grade bar) |
| PASS + Impact unclear | Insufficient verification | → Strengthen assertions |
| FAIL | Issue occurred | → Fix PoC (max 3 attempts) |

### 3. Impact Verification

```
Scenario Impact → Assertion Mapping
─────────────────────────────────────
"Vault drain"     → assertGt(attackerAfter, attackerBefore)
"Collateral loss" → assertLt(userAfter, userBefore)
"Admin bypass"    → assertTrue(attacker.hasRole(ADMIN))
```

### 4. Modification Workflow

```
Test failure or Impact mismatch
          ↓
    Fix PoC (max 3 attempts)
          ↓
    After 3 failures → Review Attack Scenario
          ↓
    If scenario needs update → Reverse-modify
```

**If PoC validation differs from scenario, update Attack Scenario**:
- Expected profit ≠ actual profit → Update Impact numbers
- Additional precondition needed → Update Preconditions
- Exploit impossible → Downgrade severity or mark Invalid

### 5. Output

**Files** (use same naming format `{Severity}-{id}-{title}`):
```
test/poc/H-01-donation-attack.t.sol            # PoC code (project root)
.vigilo/poc/H-01-donation-attack.md            # Validation log
.vigilo/findings/high/logic/H-01-donation-attack.md  # (Modified) Attack Scenario
```

**Naming Convention**: `{Severity}-{id}-{kebab-case-title}`
- Same format across all three file types
- Makes it easy to track: scenario → PoC code → validation log

### 6. Differential Verification (confirmed-differential)

A passing PoC proves the test passes. Prove it proves the BUG by showing your own fix kills it:

1. **Author a minimal `fix_patch`** - usually ONE line in the vulnerable function (check added,
   ordering moved, rounding clamped). Record it as `{path, old, new}` in the validation log.
2. **Disposable copy only**: `cp -r <project> /tmp/poc-diff-<id>`, apply the patch to the COPY,
   `forge build` there, rerun the SAME PoC file against it.
3. **The differential bar** - all three must hold, else the test is tautological and stays
   `confirmed-executable` at best:
   - baseline run: PoC PASSES (exploit reproduces)
   - patched run: build still succeeds (fix is not "break the contract")
   - patched run: PoC FAILS / exploit blocked
4. **Record both command outcomes** (commands + result lines) in the validation log and set the
   finding's ladder status: `suspected` -> `confirmed-executable` (PoC passes) ->
   `confirmed-differential` (this section) -> `reproduced` (live anvil fork, openaudit Step 6.5).
5. Never edit the real workspace or commit the patch anywhere - the fix exists to test the
   evidence, not to propose a PR.

**Why bother:** triage closes "theoretical" reports far less when the PoC itself demonstrates
that a one-line guard neutralizes the exploit - it pre-answers the exact objection.

---

## Reference Files

| File | When to Load |
|------|--------------|
| [poc-templates.md](references/poc-templates.md) | When writing PoC - Bug class patterns |
| [debugging.md](references/debugging.md) | When test fails - Error resolution |

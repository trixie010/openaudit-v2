// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

interface IBeacon {
    function implementation() external view returns (address);
}

/// @notice Base for address-target PoCs. Fails loudly unless a real fork is active.
///         Inherit this, call `_selectFork` in setUp(), then assert the impact on the
///         deployed address - never redeploy the target with mocks.
abstract contract ForkBase is Test {
    bytes32 internal constant EIP1967_IMPL_SLOT =
        0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;
    bytes32 internal constant EIP1967_BEACON_SLOT =
        0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50;
    bytes32 internal constant EIP1822_PROXIABLE_SLOT =
        0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7;

    uint256 internal forkId;
    uint256 internal forkBlock;
    uint256 internal forkChainId;

    /// @param rpcEnv env var holding the RPC URL (e.g. "JSON_RPC_ETHEREUM")
    /// @param chainId_ expected chain id (1 = mainnet, 8453 = Base, ...)
    /// @param blockNumber_ pinned snapshot block
    function _selectFork(string memory rpcEnv, uint256 chainId_, uint256 blockNumber_) internal {
        string memory rpc = vm.envString(rpcEnv);
        require(bytes(rpc).length > 0, "FORK: empty RPC env");
        forkId = vm.createSelectFork(rpc, blockNumber_);
        forkBlock = blockNumber_;
        forkChainId = chainId_;
        require(vm.activeFork() == forkId, "FORK: not active");
        require(block.chainid == chainId_, "FORK: wrong chain id");
        require(block.number == blockNumber_, "FORK: wrong block");
    }

    function _assertDeployed(address target) internal view {
        require(target.code.length > 0, "FORK: no code at target (wrong chain/address?)");
    }

    function _assertCodeHash(address target, bytes32 expected) internal view {
        require(keccak256(target.code) == expected, "FORK: runtime code hash mismatch");
    }

    function _assertProxyImpl(address proxy, address expected) internal view {
        address impl = address(uint160(uint256(vm.load(proxy, EIP1967_IMPL_SLOT))));
        require(impl == expected, "FORK: EIP-1967 implementation mismatch");
    }

    function _assertBeaconImpl(address proxy, address expected) internal view {
        address beacon = address(uint160(uint256(vm.load(proxy, EIP1967_BEACON_SLOT))));
        require(beacon != address(0), "FORK: no EIP-1967 beacon");
        require(IBeacon(beacon).implementation() == expected, "FORK: beacon implementation mismatch");
    }

    function _assertUupsImpl(address proxy, address expected) internal view {
        address impl = address(uint160(uint256(vm.load(proxy, EIP1822_PROXIABLE_SLOT))));
        require(impl == expected, "FORK: EIP-1822 implementation mismatch");
    }
}

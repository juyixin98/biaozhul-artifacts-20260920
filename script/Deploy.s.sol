// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console} from "forge-std/Script.sol";
import {ConstantProductPool} from "../src/ConstantProductPool.sol";
import {TestERC20} from "../src/test/TestERC20.sol";

/// @title Deploy
/// @notice Deploys two test ERC-20 assets and a constant-product pool.
///         Deterministic on a fresh Anvil chain so the Anvil demo script can
///         rely on the printed addresses.
contract Deploy is Script {
    function run() external returns (TestERC20 token0, TestERC20 token1, ConstantProductPool pool) {
        uint256 pk =
            vm.envOr("DEPLOYER_KEY", uint256(0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80));
        vm.startBroadcast(pk);
        token0 = new TestERC20("Test Token 0", "TKN0");
        token1 = new TestERC20("Test Token 1", "TKN1");
        pool = new ConstantProductPool(address(token0), address(token1));
        vm.stopBroadcast();

        console.log("token0:", address(token0));
        console.log("token1:", address(token1));
        console.log("pool:  ", address(pool));
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {CPMMFactory} from "../src/CPMMFactory.sol";
import {CPMMRouter} from "../src/CPMMRouter.sol";
import {TestERC20} from "../src/mocks/TestERC20.sol";
import {CPMMPair} from "../src/CPMMPair.sol";

/// @notice Deploys factory + router and (optionally) two mintable test tokens.
/// @dev Test assets only; never use these in production.
contract Deploy is Script {
    function run() external {
        uint256 pk =
            vm.envOr("PRIVATE_KEY", uint256(0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80));
        vm.startBroadcast(pk);

        CPMMFactory factory = new CPMMFactory();
        CPMMRouter router = new CPMMRouter(address(factory));
        TestERC20 tokenA = new TestERC20("Test Token A", "TKA");
        TestERC20 tokenB = new TestERC20("Test Token B", "TKB");
        address pair = factory.createPair(address(tokenA), address(tokenB));

        vm.stopBroadcast();

        console2.log("Factory: %s", address(factory));
        console2.log("Router : %s", address(router));
        console2.log("TokenA : %s", address(tokenA));
        console2.log("TokenB : %s", address(tokenB));
        console2.log("Pair   : %s", pair);
        console2.log("token0 : %s", address(CPMMPair(pair).token0()));
        console2.log("token1 : %s", address(CPMMPair(pair).token1()));
    }
}

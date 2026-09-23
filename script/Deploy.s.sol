// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {TestToken} from "../src/TestToken.sol";

/// @notice 本地链部署脚本：部署一个测试代币 + 一个 HTLC 合约。
/// @dev    用法（在任意一条 anvil 本地链上各执行一次）：
///   TOKEN_NAME="Token A" TOKEN_SYMBOL=TKA \
///     forge script script/Deploy.s.sol:Deploy --rpc-url chain_a --broadcast --private-key $PK
contract Deploy is Script {
    function run() external {
        string memory tokenName = vm.envOr("TOKEN_NAME", string("Test Token"));
        string memory tokenSymbol = vm.envOr("TOKEN_SYMBOL", string("TST"));
        uint256 pk = vm.envUint("PRIVATE_KEY");

        vm.startBroadcast(pk);
        TestToken token = new TestToken(tokenName, tokenSymbol);
        HashTimeLock htlc = new HashTimeLock();
        vm.stopBroadcast();

        console2.log("Chain id:");
        console2.log(block.chainid);
        console2.log("TestToken deployed at:", address(token));
        console2.log("HashTimeLock deployed at:", address(htlc));
    }
}

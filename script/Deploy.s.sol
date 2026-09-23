// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {MockERC20} from "../src/MockERC20.sol";

/// @title Deploy —— 在一条本地链上部署测试资产与 HTLC 合约
/// @dev 用法（任选）：
///      forge script script/Deploy.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
///      可通过环境变量覆盖：DEPLOYER_PRIVATE_KEY / TOKEN_NAME / TOKEN_SYMBOL
contract Deploy is Script {
    function run() external {
        // anvil 第 0 个预置测试账户私钥（公开测试密钥，仅本地演示用）
        uint256 deployerPk = vm.envOr(
            "DEPLOYER_PRIVATE_KEY", uint256(0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80)
        );
        string memory tokenName = vm.envOr("TOKEN_NAME", string("Test Token"));
        string memory tokenSymbol = vm.envOr("TOKEN_SYMBOL", string("TST"));

        vm.startBroadcast(deployerPk);
        MockERC20 token = new MockERC20(tokenName, tokenSymbol);
        HashTimeLock htlc = new HashTimeLock();
        vm.stopBroadcast();

        console2.log("chain id:");
        console2.logUint(block.chainid);
        console2.log("MockERC20:  ", address(token));
        console2.log("HashTimeLock:", address(htlc));
    }
}

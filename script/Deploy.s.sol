// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/TestToken.sol";
import "../src/PaymentChannel.sol";

/// @notice 部署测试代币与支付通道合约，给付款方铸币，地址写入 deployments/local.json。
contract Deploy is Script {
    // anvil 默认账户 #0（付款方）
    uint256 constant DEFAULT_PAYER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    function run() external {
        uint256 payerKey = vm.envOr("PAYER_KEY", DEFAULT_PAYER_KEY);
        uint256 challengePeriod = vm.envOr("CHALLENGE_PERIOD", uint256(1 days));
        address payer = vm.addr(payerKey);

        vm.startBroadcast(payerKey);
        TestToken token = new TestToken();
        PaymentChannel channel = new PaymentChannel(address(token), challengePeriod);
        token.mint(payer, 1_000_000 ether);
        vm.stopBroadcast();

        string memory json = "deploy";
        vm.serializeAddress(json, "token", address(token));
        string memory out = vm.serializeAddress(json, "channel", address(channel));
        vm.writeJson(out, "./deployments/local.json");

        console2.log("TestToken:     ", address(token));
        console2.log("PaymentChannel:", address(channel));
        console2.log("payer:         ", payer);
        console2.log("challengePeriod:", challengePeriod);
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/TestToken.sol";
import "../src/PaymentChannel.sol";

/// @notice 付款方开通通道并锁定抵押代币，channelId 写入 examples/channel.json。
contract Open is Script {
    uint256 constant DEFAULT_PAYER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    uint256 constant DEFAULT_PAYEE_KEY = 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d;

    function run() external {
        uint256 payerKey = vm.envOr("PAYER_KEY", DEFAULT_PAYER_KEY);
        address payee = vm.envOr("PAYEE", vm.addr(vm.envOr("PAYEE_KEY", DEFAULT_PAYEE_KEY)));
        uint256 collateral = vm.envOr("COLLATERAL", uint256(100 ether));
        bytes32 salt = keccak256(bytes(vm.envOr("SALT", string("demo-channel-1"))));

        address channelAddr = vm.parseJsonAddress(vm.readFile("./deployments/local.json"), ".channel");
        address tokenAddr = vm.parseJsonAddress(vm.readFile("./deployments/local.json"), ".token");
        PaymentChannel channel = PaymentChannel(channelAddr);

        vm.startBroadcast(payerKey);
        TestToken(tokenAddr).approve(channelAddr, collateral);
        bytes32 channelId = channel.openChannel(payee, collateral, salt);
        vm.stopBroadcast();

        string memory json = "channel";
        vm.serializeBytes32(json, "channelId", channelId);
        vm.serializeAddress(json, "payee", payee);
        string memory out = vm.serializeUint(json, "collateral", collateral);
        vm.writeJson(out, "./examples/channel.json");

        console2.log("channelId:");
        console2.logBytes32(channelId);
        console2.log("collateral:", collateral);
    }
}

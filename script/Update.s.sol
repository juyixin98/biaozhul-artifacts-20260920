// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/PaymentChannel.sol";

/// @notice 「更新」= 链下产生一份新的双方签名状态（不上链）。
///         从 examples/state-input.json 读取 {amount, nonce}，channelId 取自
///         examples/channel.json，用双方本地私钥对 EIP-712 摘要签名，
///         结果写入 examples/state-signed.json。
contract Update is Script {
    uint256 constant DEFAULT_PAYER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    uint256 constant DEFAULT_PAYEE_KEY = 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d;

    function run() external {
        uint256 payerKey = vm.envOr("PAYER_KEY", DEFAULT_PAYER_KEY);
        uint256 payeeKey = vm.envOr("PAYEE_KEY", DEFAULT_PAYEE_KEY);

        address channelAddr = vm.parseJsonAddress(vm.readFile("./deployments/local.json"), ".channel");
        bytes32 channelId = vm.parseJsonBytes32(vm.readFile("./examples/channel.json"), ".channelId");

        string memory input = vm.readFile("./examples/state-input.json");
        PaymentChannel.State memory s = PaymentChannel.State({
            channelId: channelId,
            amount: vm.parseJsonUint(input, ".amount"),
            nonce: uint64(vm.parseJsonUint(input, ".nonce"))
        });

        // 链下签名：摘要绑定 通道ID / 链ID / 累计额 / 序号（经 EIP-712 域分隔符还绑定合约地址）
        bytes32 digest = PaymentChannel(channelAddr).hashState(s);
        (uint8 v, bytes32 r, bytes32 sv) = vm.sign(payerKey, digest);
        bytes memory sigPayer = abi.encodePacked(r, sv, v);
        (v, r, sv) = vm.sign(payeeKey, digest);
        bytes memory sigPayee = abi.encodePacked(r, sv, v);

        string memory json = "state";
        vm.serializeBytes32(json, "channelId", s.channelId);
        vm.serializeUint(json, "amount", s.amount);
        vm.serializeUint(json, "nonce", s.nonce);
        vm.serializeBytes(json, "sigPayer", sigPayer);
        string memory out = vm.serializeBytes(json, "sigPayee", sigPayee);
        vm.writeJson(out, "./examples/state-signed.json");

        console2.log("signed state -> examples/state-signed.json");
        console2.log("amount:", s.amount);
        console2.log("nonce: ", s.nonce);
        console2.logBytes32(digest);
    }
}

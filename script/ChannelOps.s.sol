// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/PaymentChannel.sol";

/// @notice 读取 examples/state-signed.json 中的双方签名状态。
abstract contract StateReader is Script {
    uint256 constant DEFAULT_PAYER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    uint256 constant DEFAULT_PAYEE_KEY = 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d;

    function _load()
        internal
        returns (PaymentChannel channel, PaymentChannel.State memory s, bytes memory sigPayer, bytes memory sigPayee)
    {
        channel = PaymentChannel(vm.parseJsonAddress(vm.readFile("./deployments/local.json"), ".channel"));
        string memory raw = vm.readFile("./examples/state-signed.json");
        s = PaymentChannel.State({
            channelId: vm.parseJsonBytes32(raw, ".channelId"),
            amount: vm.parseJsonUint(raw, ".amount"),
            nonce: uint64(vm.parseJsonUint(raw, ".nonce"))
        });
        sigPayer = vm.parseJsonBytes(raw, ".sigPayer");
        sigPayee = vm.parseJsonBytes(raw, ".sigPayee");
    }

    function _payerKey() internal view returns (uint256) {
        return vm.envOr("PAYER_KEY", DEFAULT_PAYER_KEY);
    }

    function _payeeKey() internal view returns (uint256) {
        return vm.envOr("PAYEE_KEY", DEFAULT_PAYEE_KEY);
    }
}

/// @notice 用 examples/state-signed.json 中的状态关闭通道（可以是旧状态），进入挑战期。
///         由收款方提交（CLOSER=payer 可改为付款方）。
contract Close is StateReader {
    function run() external {
        (PaymentChannel channel, PaymentChannel.State memory s, bytes memory sigPayer, bytes memory sigPayee) =
            _load();
        uint256 closerKey = keccak256(bytes(vm.envOr("CLOSER", string("payee")))) == keccak256("payer")
            ? _payerKey()
            : _payeeKey();

        vm.broadcast(closerKey);
        channel.closeChannel(s, sigPayer, sigPayee);

        console2.log("closed with nonce:", s.nonce);
        console2.log("closed with amount:", s.amount);
    }
}

/// @notice 挑战期内用 examples/state-signed.json 中更高序号的状态覆盖链上记录。
contract Challenge is StateReader {
    function run() external {
        (PaymentChannel channel, PaymentChannel.State memory s, bytes memory sigPayer, bytes memory sigPayee) =
            _load();

        vm.broadcast(_payeeKey());
        channel.challenge(s, sigPayer, sigPayee);

        console2.log("challenged with nonce:", s.nonce);
        console2.log("challenged with amount:", s.amount);
    }
}

/// @notice 挑战期到期后结算通道（任何人可调用）。
contract Settle is StateReader {
    function run() external {
        (PaymentChannel channel,,,) = _load();
        bytes32 channelId = vm.parseJsonBytes32(vm.readFile("./examples/channel.json"), ".channelId");

        vm.broadcast(_payerKey());
        channel.settle(channelId);

        console2.log("settled channel:");
        console2.logBytes32(channelId);
    }
}

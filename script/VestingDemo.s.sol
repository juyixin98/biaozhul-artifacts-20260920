// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {VestingStream} from "../src/VestingStream.sol";
import {MockERC20} from "../test/mocks/MockERC20.sol";

/// @notice 本地 Anvil 演示脚本：部署代币与归属合约，创建一条归属流并补足资金。
///         参数可用环境变量覆盖，默认值见 examples/demo-inputs.json。
///
///         用法：
///           anvil &
///           forge script script/VestingDemo.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
contract VestingDemo is Script {
    // Anvil 默认账户 #0 / #1
    uint256 constant DEFAULT_SENDER_KEY = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    address constant DEFAULT_BENEFICIARY = 0x70997970C51812dc3A010C7d01b50e0d17dc79C8;

    function run() external {
        uint256 senderKey = vm.envOr("SENDER_KEY", DEFAULT_SENDER_KEY);
        address beneficiary = vm.envOr("BENEFICIARY", DEFAULT_BENEFICIARY);
        uint128 amount = uint128(vm.envOr("AMOUNT", uint256(1_000_000 ether)));
        uint64 startDelta = uint64(vm.envOr("START_DELTA", uint256(0)));
        uint64 cliffDelta = uint64(vm.envOr("CLIFF_DELTA", uint256(1 hours)));
        uint64 duration = uint64(vm.envOr("DURATION", uint256(4 hours)));
        uint128 topUpExtra = uint128(vm.envOr("TOPUP_EXTRA", uint256(250_000 ether)));

        address sender = vm.addr(senderKey);

        vm.startBroadcast(senderKey);
        MockERC20 token = new MockERC20("Vesting Token", "VST");
        VestingStream vesting = new VestingStream(token);

        token.mint(sender, uint256(amount) * 2);
        token.approve(address(vesting), type(uint256).max);

        uint64 start = uint64(block.timestamp) + startDelta;
        uint64 cliff = start + cliffDelta;
        uint64 end = start + duration;
        uint256 id = vesting.createStream(beneficiary, amount, start, cliff, end);
        vesting.topUp(id, topUpExtra);
        vm.stopBroadcast();

        console2.log("token        :", address(token));
        console2.log("vesting      :", address(vesting));
        console2.log("stream id    :", id);
        console2.log("sender       :", sender);
        console2.log("beneficiary  :", beneficiary);
        console2.log("total amount :", uint256(amount) + topUpExtra);
        console2.log("start/cliff/end:", start, cliff, end);
    }
}

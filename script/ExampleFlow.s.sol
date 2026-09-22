// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {StreamVesting} from "../src/StreamVesting.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";

/// @notice 端到端示例：读取 examples/sample-input.json，走一遍
///         建流 → 补足 → 领取 → 撤销 → 受益人领取保留额 的完整流程并打印各阶段余额。
/// 运行：forge script script/ExampleFlow.s.sol -vvv
contract ExampleFlow is Script {
    function run() external {
        string memory json = vm.readFile("examples/sample-input.json");
        string memory name = vm.parseJsonString(json, ".tokenName");
        string memory symbol = vm.parseJsonString(json, ".tokenSymbol");
        uint128 amount = uint128(vm.parseJsonUint(json, ".amount"));
        uint64 start = uint64(vm.parseJsonUint(json, ".start"));
        uint64 cliff = uint64(vm.parseJsonUint(json, ".cliff"));
        uint64 end = uint64(vm.parseJsonUint(json, ".end"));
        uint64 withdrawAt = uint64(vm.parseJsonUint(json, ".withdrawAt"));
        uint64 topUpAt = uint64(vm.parseJsonUint(json, ".topUpAt"));
        uint128 topUpExtra = uint128(vm.parseJsonUint(json, ".topUpExtra"));
        uint64 cancelAt = uint64(vm.parseJsonUint(json, ".cancelAt"));

        address sender = makeAddr("sender");
        address beneficiary = makeAddr("beneficiary");

        MockERC20 token = new MockERC20(name, symbol);
        StreamVesting vesting = new StreamVesting(token);
        token.mint(sender, uint256(amount) * 10);
        vm.prank(sender);
        token.approve(address(vesting), type(uint256).max);

        console2.log("== create ==");
        vm.warp(start);
        vm.prank(sender);
        uint256 id = vesting.createStream(beneficiary, amount, start, cliff, end);
        _report(id, sender, beneficiary, token, vesting);

        console2.log("== topUp at", topUpAt, "==");
        vm.warp(topUpAt);
        vm.prank(sender);
        vesting.topUp(id, topUpExtra);
        _report(id, sender, beneficiary, token, vesting);

        console2.log("== withdraw at", withdrawAt, "==");
        vm.warp(withdrawAt);
        vm.prank(beneficiary);
        vesting.withdraw(id);
        _report(id, sender, beneficiary, token, vesting);

        console2.log("== cancel at", cancelAt, "==");
        vm.warp(cancelAt);
        vm.prank(sender);
        vesting.cancel(id);
        _report(id, sender, beneficiary, token, vesting);

        console2.log("== beneficiary withdraws kept vested ==");
        vm.prank(beneficiary);
        vesting.withdraw(id);
        _report(id, sender, beneficiary, token, vesting);
    }

    function _report(uint256 id, address sender, address beneficiary, MockERC20 token, StreamVesting vesting)
        internal
        view
    {
        console2.log("  t                 :", block.timestamp);
        console2.log("  vested(now)       :", vesting.vestedOf(id, uint64(block.timestamp)));
        console2.log("  withdrawable      :", vesting.withdrawable(id));
        console2.log("  contract balance  :", token.balanceOf(address(vesting)));
        console2.log("  sender balance    :", token.balanceOf(sender));
        console2.log("  beneficiary bal.  :", token.balanceOf(beneficiary));
    }
}

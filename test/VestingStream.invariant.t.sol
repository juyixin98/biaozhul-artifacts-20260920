// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {VestingStream} from "../src/VestingStream.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {VestingHandler} from "./VestingHandler.sol";

/// @notice 状态机不变量测试：随机操作序列驱动合约，
///         每一步都与 Handler 内的独立模型核对余额、事件与可领取额。
contract VestingStreamInvariantTest is Test {
    MockERC20 token;
    VestingStream vesting;
    VestingHandler handler;

    function setUp() public {
        token = new MockERC20("Test Token", "TT");
        vesting = new VestingStream(token);
        handler = new VestingHandler(vesting, token);
        targetContract(address(handler));
    }

    /// @notice 资产守恒：合约余额 == 累计存入 - 累计领取 - 累计退款。
    function invariant_conservation() public view {
        uint256 expected = handler.ghost_deposited() - handler.ghost_claimed() - handler.ghost_refunded();
        assertEq(token.balanceOf(address(vesting)), expected, unicode"合约托管余额不守恒");
    }

    /// @notice 每条流的可领取额与独立模型一致。
    function invariant_claimableMatchesModel() public view {
        uint256 n = handler.streamCount();
        for (uint256 i = 0; i < n; i++) {
            assertEq(vesting.claimable(i), handler.modelClaimable(i), unicode"可领取额与模型不一致");
        }
    }

    /// @notice 合约存储的每条流状态与模型一致。
    function invariant_stateMatchesModel() public view {
        uint256 n = handler.streamCount();
        for (uint256 i = 0; i < n; i++) {
            VestingStream.Stream memory s = vesting.getStream(i);
            VestingHandler.MStream memory m = handler.getModel(i);
            assertEq(s.sender, m.sender);
            assertEq(s.beneficiary, m.beneficiary);
            assertEq(s.amount, m.amount, unicode"amount 与模型不一致");
            assertEq(s.claimed, m.claimed, unicode"claimed 与模型不一致");
            assertEq(s.refunded, m.refunded, unicode"refunded 与模型不一致");
            assertEq(s.start, m.start);
            assertEq(s.cliff, m.cliff);
            assertEq(s.end, m.end);
            assertEq(s.canceledAt, m.canceledAt, unicode"canceledAt 与模型不一致");
        }
    }

    /// @notice 各 actor 的代币余额 == 初始 + 增发 - 流出 + 流入。
    function invariant_actorBalances() public view {
        address[] memory senders = handler.getSenders();
        address[] memory beneficiaries = handler.getBeneficiaries();
        for (uint256 i = 0; i < senders.length; i++) {
            address a = senders[i];
            uint256 expected = handler.ghost_minted(a) - handler.ghost_out(a) + handler.ghost_in(a);
            assertEq(token.balanceOf(a), expected, unicode"sender 余额与模型不一致");
        }
        for (uint256 i = 0; i < beneficiaries.length; i++) {
            address a = beneficiaries[i];
            uint256 expected = handler.ghost_minted(a) - handler.ghost_out(a) + handler.ghost_in(a);
            assertEq(token.balanceOf(a), expected, unicode"beneficiary 余额与模型不一致");
        }
    }

    /// @notice 已领取总额永不超过已归属总额（按流）。
    function invariant_claimedNeverExceedsVested() public view {
        uint256 n = handler.streamCount();
        for (uint256 i = 0; i < n; i++) {
            VestingStream.Stream memory s = vesting.getStream(i);
            assertLe(s.claimed, vesting.vested(i), unicode"已领取超过已归属");
            assertLe(uint256(s.claimed) + s.refunded, s.amount, unicode"领取+退款超过存入");
        }
    }
}

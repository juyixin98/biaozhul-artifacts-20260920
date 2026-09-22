// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {StreamVesting} from "../src/StreamVesting.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";
import {ReentrantActor} from "../src/mocks/ReentrantActor.sol";

/// @notice 单元测试：状态机迁移、权限、事件、整数余量结清、回调重入、同时间重复调用。
contract StreamVestingTest is Test {
    MockERC20 internal token;
    StreamVesting internal vesting;

    address internal sender = makeAddr("sender");
    address internal beneficiary = makeAddr("beneficiary");
    address internal stranger = makeAddr("stranger");

    // start=1000, cliff=1500, end=2000, amount=1_000_003（duration=1000，余量 3）
    uint64 internal constant START = 1000;
    uint64 internal constant CLIFF = 1500;
    uint64 internal constant END = 2000;
    uint128 internal constant AMOUNT = 1_000_003;

    function setUp() public {
        token = new MockERC20("Mock", "MCK");
        vesting = new StreamVesting(token);
        token.mint(sender, 1e30);
        vm.prank(sender);
        token.approve(address(vesting), type(uint256).max);
        vm.warp(START);
    }

    function _create() internal returns (uint256 id) {
        vm.prank(sender);
        id = vesting.createStream(beneficiary, AMOUNT, START, CLIFF, END);
    }

    // ---------------------------------------------------------------
    // 创建：参数校验
    // ---------------------------------------------------------------

    function test_CreateEmitsEvent() public {
        vm.expectEmit(true, true, true, true);
        emit StreamVesting.StreamCreated(0, sender, beneficiary, AMOUNT, START, CLIFF, END);
        uint256 id = _create();
        assertEq(id, 0);
        (address s, address b, uint128 amount, uint128 withdrawn, uint64 st, uint64 cl, uint64 en, bool cx) =
            vesting.streams(id);
        assertEq(s, sender);
        assertEq(b, beneficiary);
        assertEq(amount, AMOUNT);
        assertEq(withdrawn, 0);
        assertEq(st, START);
        assertEq(cl, CLIFF);
        assertEq(en, END);
        assertFalse(cx);
        assertEq(token.balanceOf(address(vesting)), AMOUNT);
    }

    function test_CreateRejectsZeroDuration() public {
        vm.prank(sender);
        vm.expectRevert(StreamVesting.ZeroDuration.selector);
        vesting.createStream(beneficiary, AMOUNT, START, START, START);
    }

    function test_CreateRejectsInvertedTimes() public {
        vm.prank(sender);
        vm.expectRevert(StreamVesting.InvertedTimes.selector);
        vesting.createStream(beneficiary, AMOUNT, END, CLIFF, START);
    }

    function test_CreateRejectsCliffBeforeStart() public {
        vm.prank(sender);
        vm.expectRevert(StreamVesting.CliffBeforeStart.selector);
        vesting.createStream(beneficiary, AMOUNT, START, START - 1, END);
    }

    function test_CreateRejectsCliffAfterEnd() public {
        vm.prank(sender);
        vm.expectRevert(StreamVesting.CliffAfterEnd.selector);
        vesting.createStream(beneficiary, AMOUNT, START, END + 1, END);
    }

    function test_CreateRejectsZeroAmount() public {
        vm.prank(sender);
        vm.expectRevert(StreamVesting.ZeroAmount.selector);
        vesting.createStream(beneficiary, 0, START, CLIFF, END);
    }

    function test_CreateRejectsZeroBeneficiary() public {
        vm.prank(sender);
        vm.expectRevert(StreamVesting.ZeroAddress.selector);
        vesting.createStream(address(0), AMOUNT, START, CLIFF, END);
    }

    function test_CreateRejectsEndInPast() public {
        vm.warp(END + 1);
        vm.prank(sender);
        vm.expectRevert(StreamVesting.EndInPast.selector);
        vesting.createStream(beneficiary, AMOUNT, START, CLIFF, END);
    }

    // ---------------------------------------------------------------
    // 归属曲线：cliff / 线性 / 余量结清
    // ---------------------------------------------------------------

    function test_VestedIsZeroBeforeCliff() public {
        uint256 id = _create();
        assertEq(vesting.vestedOf(id, START), 0);
        assertEq(vesting.vestedOf(id, CLIFF - 1), 0);
    }

    function test_VestedLinearBetweenCliffAndEnd() public {
        uint256 id = _create();
        // t=1750: elapsed=750/1000 → floor(1_000_003 * 750 / 1000) = 750_002
        assertEq(vesting.vestedOf(id, 1750), 750_002);
        // cliff 时刻即开始归属: elapsed=500 → 500_001
        assertEq(vesting.vestedOf(id, CLIFF), 500_001);
    }

    function test_VestedSettlesRemainderAtEnd() public {
        uint256 id = _create();
        // t=1999: floor(1_000_003 * 999 / 1000) = 999_002（差 1 未到全额）
        assertEq(vesting.vestedOf(id, END - 1), 999_002);
        // t=end: 余量结清，精确等于全额
        assertEq(vesting.vestedOf(id, END), AMOUNT);
        assertEq(vesting.vestedOf(id, END + 500), AMOUNT);
    }

    // ---------------------------------------------------------------
    // 领取
    // ---------------------------------------------------------------

    function test_WithdrawPaysBeneficiaryAndEmits() public {
        uint256 id = _create();
        vm.warp(1750);

        vm.expectEmit(true, true, false, true);
        emit StreamVesting.Withdrawn(id, beneficiary, 750_002);
        vm.prank(beneficiary);
        vesting.withdraw(id);

        assertEq(token.balanceOf(beneficiary), 750_002);
        assertEq(token.balanceOf(address(vesting)), AMOUNT - 750_002);
        assertEq(vesting.withdrawable(id), 0);
        (, , , uint128 withdrawn, , , , ) = vesting.streams(id);
        assertEq(withdrawn, 750_002);
    }

    function test_WithdrawOnlyBeneficiary() public {
        uint256 id = _create();
        vm.warp(1750);
        vm.prank(stranger);
        vm.expectRevert(StreamVesting.NotBeneficiary.selector);
        vesting.withdraw(id);
    }

    function test_WithdrawBeforeCliffReverts() public {
        uint256 id = _create();
        vm.prank(beneficiary);
        vm.expectRevert(StreamVesting.NothingToWithdraw.selector);
        vesting.withdraw(id);
    }

    function test_WithdrawTwiceSameTimestampReverts() public {
        uint256 id = _create();
        vm.warp(1750);
        vm.prank(beneficiary);
        vesting.withdraw(id);
        // 同一区块（时间未推进）再次领取：无可领取额
        vm.prank(beneficiary);
        vm.expectRevert(StreamVesting.NothingToWithdraw.selector);
        vesting.withdraw(id);
    }

    function test_WithdrawAccumulatesAcrossTime() public {
        uint256 id = _create();
        vm.warp(1750);
        vm.prank(beneficiary);
        vesting.withdraw(id); // 750_002

        vm.warp(END);
        vm.prank(beneficiary);
        vesting.withdraw(id); // 剩余 250_001（含余量结清）

        assertEq(token.balanceOf(beneficiary), AMOUNT);
        assertEq(token.balanceOf(address(vesting)), 0);
    }

    // ---------------------------------------------------------------
    // 撤销
    // ---------------------------------------------------------------

    function test_CancelRefundsOnlyUnvested() public {
        uint256 id = _create();
        vm.warp(1750);
        uint256 senderBefore = token.balanceOf(sender);

        vm.expectEmit(true, false, false, true);
        emit StreamVesting.StreamCancelled(id, 250_001, 750_002);
        vm.prank(sender);
        vesting.cancel(id);

        // 发送者收回未归属部分
        assertEq(token.balanceOf(sender), senderBefore + 250_001);
        // 受益人保留已归属未领取额
        assertEq(vesting.withdrawable(id), 750_002);
        assertEq(token.balanceOf(address(vesting)), 750_002);

        vm.prank(beneficiary);
        vesting.withdraw(id);
        assertEq(token.balanceOf(beneficiary), 750_002);
        assertEq(token.balanceOf(address(vesting)), 0);
    }

    function test_CancelBeforeCliffRefundsEverything() public {
        uint256 id = _create();
        vm.warp(1200);
        uint256 senderBefore = token.balanceOf(sender);
        vm.prank(sender);
        vesting.cancel(id);
        assertEq(token.balanceOf(sender), senderBefore + AMOUNT);
        assertEq(token.balanceOf(address(vesting)), 0);
        vm.prank(beneficiary);
        vm.expectRevert(StreamVesting.NothingToWithdraw.selector);
        vesting.withdraw(id);
    }

    function test_CancelAfterEndRefundsZero() public {
        uint256 id = _create();
        vm.warp(END + 100);
        uint256 senderBefore = token.balanceOf(sender);
        vm.prank(sender);
        vesting.cancel(id);
        assertEq(token.balanceOf(sender), senderBefore);
        // 受益人仍可领取全额（含余量）
        vm.prank(beneficiary);
        vesting.withdraw(id);
        assertEq(token.balanceOf(beneficiary), AMOUNT);
    }

    function test_CancelOnlySender() public {
        uint256 id = _create();
        vm.prank(stranger);
        vm.expectRevert(StreamVesting.NotSender.selector);
        vesting.cancel(id);
    }

    function test_CancelTwiceReverts() public {
        uint256 id = _create();
        vm.warp(1750);
        vm.prank(sender);
        vesting.cancel(id);
        // 同一时间重复撤销
        vm.prank(sender);
        vm.expectRevert(StreamVesting.StreamAlreadyCancelled.selector);
        vesting.cancel(id);
    }

    // ---------------------------------------------------------------
    // 资金补足
    // ---------------------------------------------------------------

    function test_TopUpIncreasesVestedAndConserves() public {
        uint256 id = _create();
        vm.warp(1600);
        uint128 extra = 500_000;

        vm.expectEmit(true, false, false, true);
        emit StreamVesting.StreamToppedUp(id, extra, AMOUNT + extra);
        vm.prank(sender);
        vesting.topUp(id, extra);

        // 总额提高，时间表不变：t=1750 → floor(1_500_003 * 750 / 1000) = 1_125_002
        vm.warp(1750);
        assertEq(vesting.vestedOf(id, 1750), 1_125_002);
        assertEq(token.balanceOf(address(vesting)), AMOUNT + extra);

        vm.warp(END);
        vm.prank(beneficiary);
        vesting.withdraw(id);
        assertEq(token.balanceOf(beneficiary), AMOUNT + extra);
        assertEq(token.balanceOf(address(vesting)), 0);
    }

    function test_TopUpOnlySender() public {
        uint256 id = _create();
        token.mint(stranger, 100);
        vm.prank(stranger);
        token.approve(address(vesting), 100);
        vm.prank(stranger);
        vm.expectRevert(StreamVesting.NotSender.selector);
        vesting.topUp(id, 100);
    }

    function test_TopUpAfterEndReverts() public {
        uint256 id = _create();
        vm.warp(END);
        vm.prank(sender);
        vm.expectRevert(StreamVesting.StreamAlreadyEnded.selector);
        vesting.topUp(id, 100);
    }

    function test_TopUpAfterCancelReverts() public {
        uint256 id = _create();
        vm.warp(1600);
        vm.prank(sender);
        vesting.cancel(id);
        vm.prank(sender);
        vm.expectRevert(StreamVesting.StreamAlreadyCancelled.selector);
        vesting.topUp(id, 100);
    }

    // ---------------------------------------------------------------
    // 回调重入
    // ---------------------------------------------------------------

    function test_ReentrancyOnWithdrawIsBlocked() public {
        ReentrantActor attacker = new ReentrantActor(vesting, token);
        token.setCallback(address(attacker), true);

        vm.prank(sender);
        uint256 id = vesting.createStream(address(attacker), AMOUNT, START, CLIFF, END);

        vm.warp(1750);
        attacker.setAttack(ReentrantActor.Attack.Withdraw, id);
        vm.expectRevert(StreamVesting.Reentrant.selector);
        attacker.withdraw(id);

        // 攻击失败后状态未被破坏：解除攻击可正常领取
        attacker.setAttack(ReentrantActor.Attack.None, id);
        attacker.withdraw(id);
        assertEq(token.balanceOf(address(attacker)), 750_002);
        assertEq(vesting.withdrawable(id), 0);
    }

    function test_ReentrancyOnCancelIsBlocked() public {
        ReentrantActor attacker = new ReentrantActor(vesting, token);
        token.setCallback(address(attacker), true);
        token.mint(address(attacker), AMOUNT);

        uint256 id = attacker.createStream(beneficiary, AMOUNT, START, CLIFF, END);

        vm.warp(1750);
        attacker.setAttack(ReentrantActor.Attack.Cancel, id);
        vm.expectRevert(StreamVesting.Reentrant.selector);
        attacker.cancel(id);

        // 解除攻击后可正常撤销，退款金额正确
        attacker.setAttack(ReentrantActor.Attack.None, id);
        attacker.cancel(id);
        assertEq(token.balanceOf(address(attacker)), 250_001);
        assertEq(vesting.withdrawable(id), 750_002);
    }

    // ---------------------------------------------------------------
    // 模糊测试：归属曲线对照独立模型
    // ---------------------------------------------------------------

    /// @dev 独立模型：rate/rem 拆分计算，与合约的单表达式除法是不同计算路径。
    function _modelVested(uint128 amount, uint64 start, uint64 cliff, uint64 end, uint64 t)
        internal
        pure
        returns (uint128)
    {
        if (t < cliff) return 0;
        uint64 duration = end - start;
        uint64 elapsed = t >= end ? duration : t - start;
        uint256 rate = uint256(amount) / duration;
        uint256 rem = uint256(amount) % duration;
        return uint128(rate * elapsed + (rem * elapsed) / duration);
    }

    function testFuzz_VestedMatchesIndependentModel(
        uint128 amount,
        uint64 duration,
        uint64 cliffDelta,
        uint64 t
    ) public {
        amount = uint128(bound(uint256(amount), 1, 1e30));
        duration = uint64(bound(uint256(duration), 1, 1_000_000));
        uint64 start = uint64(bound(uint256(t), 1, 1e9));
        uint64 end = start + duration;
        cliffDelta = uint64(bound(uint256(cliffDelta), 0, duration));
        uint64 cliff = start + cliffDelta;
        uint64 at = uint64(bound(uint256(t) * 7 + 13, 0, 2e9));

        token.mint(sender, amount);
        vm.warp(start);
        vm.prank(sender);
        uint256 id = vesting.createStream(beneficiary, amount, start, cliff, end);

        assertEq(vesting.vestedOf(id, at), _modelVested(amount, start, cliff, end, at));
        // 结束时余量结清
        assertEq(vesting.vestedOf(id, end), amount);
    }
}

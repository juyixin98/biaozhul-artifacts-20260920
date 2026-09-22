// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {VestingStream} from "../src/VestingStream.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {CallbackToken} from "./mocks/CallbackToken.sol";
import {ReentrantClaimer} from "./mocks/ReentrantClaimer.sol";

contract VestingStreamTest is Test {
    MockERC20 token;
    VestingStream vesting;

    address sender = makeAddr("sender");
    address beneficiary = makeAddr("beneficiary");
    address stranger = makeAddr("stranger");

    uint64 constant T0 = 10_000;
    uint128 constant AMOUNT = 1_000_000;

    function setUp() public {
        token = new MockERC20("Test Token", "TT");
        vesting = new VestingStream(token);
        token.mint(sender, 100e24);
        vm.prank(sender);
        token.approve(address(vesting), type(uint256).max);
        vm.warp(T0);
    }

    function _defaultStream() internal returns (uint256 id) {
        vm.prank(sender);
        id = vesting.createStream(beneficiary, AMOUNT, T0, T0 + 100, T0 + 1000);
    }

    // ------------------------------------------------------------------
    // 创建参数校验
    // ------------------------------------------------------------------

    function test_CreateRejectsZeroAddress() public {
        vm.prank(sender);
        vm.expectRevert(VestingStream.ZeroAddress.selector);
        vesting.createStream(address(0), AMOUNT, T0, T0, T0 + 1);
    }

    function test_CreateRejectsZeroAmount() public {
        vm.prank(sender);
        vm.expectRevert(VestingStream.ZeroAmount.selector);
        vesting.createStream(beneficiary, 0, T0, T0, T0 + 1);
    }

    function test_CreateRejectsZeroDuration() public {
        vm.prank(sender);
        vm.expectRevert(VestingStream.ZeroDuration.selector);
        vesting.createStream(beneficiary, AMOUNT, T0, T0, T0);
    }

    function test_CreateRejectsInvertedTimes() public {
        vm.prank(sender);
        vm.expectRevert(VestingStream.InvertedTimes.selector);
        vesting.createStream(beneficiary, AMOUNT, T0 + 100, T0 + 100, T0);
    }

    function test_CreateRejectsCliffBeforeStart() public {
        vm.prank(sender);
        vm.expectRevert(VestingStream.InvalidCliff.selector);
        vesting.createStream(beneficiary, AMOUNT, T0 + 100, T0, T0 + 1000);
    }

    function test_CreateRejectsCliffAfterEnd() public {
        vm.prank(sender);
        vm.expectRevert(VestingStream.InvalidCliff.selector);
        vesting.createStream(beneficiary, AMOUNT, T0, T0 + 1001, T0 + 1000);
    }

    function test_CreateEmitsEventAndEscrows() public {
        vm.prank(sender);
        vm.expectEmit(true, false, false, true);
        emit VestingStream.StreamCreated(0, sender, beneficiary, AMOUNT, T0, T0 + 100, T0 + 1000);
        uint256 id = vesting.createStream(beneficiary, AMOUNT, T0, T0 + 100, T0 + 1000);
        assertEq(id, 0);
        assertEq(token.balanceOf(address(vesting)), AMOUNT);
    }

    // ------------------------------------------------------------------
    // 线性归属与余量结清
    // ------------------------------------------------------------------

    function test_VestingLinearWithCliff() public {
        uint256 id = _defaultStream();
        // cliff 之前：0
        vm.warp(T0 + 99);
        assertEq(vesting.vested(id), 0);
        // cliff 时刻：线性从 start 算起
        vm.warp(T0 + 100);
        assertEq(vesting.vested(id), AMOUNT * 100 / 1000);
        // 中点
        vm.warp(T0 + 500);
        assertEq(vesting.vested(id), AMOUNT / 2);
        // end 之后：全额
        vm.warp(T0 + 1000);
        assertEq(vesting.vested(id), AMOUNT);
        vm.warp(T0 + 999_999);
        assertEq(vesting.vested(id), AMOUNT);
    }

    function test_RemainderSettledAtEnd() public {
        // 100 / 3 除不尽：中途向下取整，end 时刻结清全部余量
        vm.prank(sender);
        uint256 id = vesting.createStream(beneficiary, 100, T0, T0, T0 + 3);
        vm.warp(T0 + 1);
        assertEq(vesting.vested(id), 33);
        vm.warp(T0 + 2);
        assertEq(vesting.vested(id), 66);
        vm.warp(T0 + 3);
        assertEq(vesting.vested(id), 100); // 余量 1 在此结清
    }

    function testFuzz_VestedMatchesReferenceFormula(uint128 amount, uint64 elapsed, uint64 duration) public {
        vm.assume(duration > 0 && duration < 1e12);
        elapsed = uint64(bound(elapsed, 0, uint64(duration) * 2));
        amount = uint128(bound(amount, 1, 100e24)); // 不超过 sender 已铸造余额
        vm.prank(sender);
        uint256 id = vesting.createStream(beneficiary, amount, T0, T0, T0 + duration);
        vm.warp(T0 + elapsed);
        uint256 expected = elapsed >= duration ? uint256(amount) : uint256(amount) * elapsed / duration;
        assertEq(vesting.vested(id), expected);
    }

    // ------------------------------------------------------------------
    // 领取
    // ------------------------------------------------------------------

    function test_ClaimOnlyBeneficiary() public {
        uint256 id = _defaultStream();
        vm.warp(T0 + 500);
        vm.prank(stranger);
        vm.expectRevert(VestingStream.NotBeneficiary.selector);
        vesting.claim(id);
        vm.prank(sender);
        vm.expectRevert(VestingStream.NotBeneficiary.selector);
        vesting.claim(id);
    }

    function test_ClaimNothingVestedReverts() public {
        uint256 id = _defaultStream();
        vm.prank(beneficiary); // 仍在 cliff 之前
        vm.expectRevert(VestingStream.NothingToClaim.selector);
        vesting.claim(id);
    }

    function test_ClaimTransfersAndEmits() public {
        uint256 id = _defaultStream();
        vm.warp(T0 + 500);
        uint256 expected = AMOUNT / 2;
        vm.prank(beneficiary);
        vm.expectEmit(true, false, false, true);
        emit VestingStream.Claimed(id, expected);
        vesting.claim(id);
        assertEq(token.balanceOf(beneficiary), expected);
        assertEq(vesting.claimable(id), 0);
    }

    function test_DoubleClaimSameTimestampReverts() public {
        uint256 id = _defaultStream();
        vm.warp(T0 + 500);
        vm.prank(beneficiary);
        vesting.claim(id);
        // 同一时间戳第二次领取：无可领额度
        vm.prank(beneficiary);
        vm.expectRevert(VestingStream.NothingToClaim.selector);
        vesting.claim(id);
    }

    // ------------------------------------------------------------------
    // 撤销
    // ------------------------------------------------------------------

    function test_CancelOnlySender() public {
        uint256 id = _defaultStream();
        vm.prank(beneficiary);
        vm.expectRevert(VestingStream.NotSender.selector);
        vesting.cancel(id);
        vm.prank(stranger);
        vm.expectRevert(VestingStream.NotSender.selector);
        vesting.cancel(id);
    }

    function test_CancelRefundsOnlyUnvested() public {
        uint256 id = _defaultStream();
        vm.warp(T0 + 500); // 归属 50%
        uint256 senderBefore = token.balanceOf(sender);
        vm.prank(sender);
        vm.expectEmit(true, false, false, true);
        emit VestingStream.Canceled(id, AMOUNT / 2, AMOUNT / 2);
        vesting.cancel(id);

        assertEq(token.balanceOf(sender), senderBefore + AMOUNT / 2, unicode"sender 收回未归属部分");
        // 受益人保留已归属未领取额度
        assertEq(vesting.claimable(id), AMOUNT / 2);
        // 撤销后时间冻结：再往后走也不增加
        vm.warp(T0 + 999_999);
        assertEq(vesting.claimable(id), AMOUNT / 2);

        vm.prank(beneficiary);
        vesting.claim(id);
        assertEq(token.balanceOf(beneficiary), AMOUNT / 2);
        assertEq(token.balanceOf(address(vesting)), 0, unicode"合约余额清零：守恒");
    }

    function test_CancelBeforeCliffRefundsEverything() public {
        uint256 id = _defaultStream();
        uint256 senderBefore = token.balanceOf(sender);
        vm.prank(sender);
        vesting.cancel(id);
        assertEq(token.balanceOf(sender), senderBefore + AMOUNT);
        assertEq(vesting.claimable(id), 0);
    }

    function test_DoubleCancelSameTimestampReverts() public {
        uint256 id = _defaultStream();
        vm.prank(sender);
        vesting.cancel(id);
        vm.prank(sender);
        vm.expectRevert(VestingStream.StreamCanceled.selector);
        vesting.cancel(id);
    }

    // ------------------------------------------------------------------
    // 资金补足
    // ------------------------------------------------------------------

    function test_TopUpOnlySender() public {
        uint256 id = _defaultStream();
        vm.prank(stranger);
        vm.expectRevert(VestingStream.NotSender.selector);
        vesting.topUp(id, 1);
        vm.prank(beneficiary);
        vm.expectRevert(VestingStream.NotSender.selector);
        vesting.topUp(id, 1);
    }

    function test_TopUpIncreasesClaimable() public {
        uint256 id = _defaultStream();
        vm.warp(T0 + 500); // 归属 500_000
        token.mint(sender, 500_000);
        vm.prank(sender);
        vm.expectEmit(true, false, false, true);
        emit VestingStream.ToppedUp(id, 500_000, AMOUNT + 500_000);
        vesting.topUp(id, 500_000);
        // 新总额 1_500_000，50% 已归属
        assertEq(vesting.claimable(id), 750_000);
        assertEq(token.balanceOf(address(vesting)), AMOUNT + 500_000);
    }

    function test_TopUpZeroReverts() public {
        uint256 id = _defaultStream();
        vm.prank(sender);
        vm.expectRevert(VestingStream.ZeroAmount.selector);
        vesting.topUp(id, 0);
    }

    function test_TopUpAfterCancelReverts() public {
        uint256 id = _defaultStream();
        vm.prank(sender);
        vesting.cancel(id);
        vm.prank(sender);
        vm.expectRevert(VestingStream.StreamCanceled.selector);
        vesting.topUp(id, 1);
    }

    // ------------------------------------------------------------------
    // 资产守恒（脚本化序列）
    // ------------------------------------------------------------------

    function test_ConservationAcrossClaimCancelTopUp() public {
        uint256 id = _defaultStream();
        uint256 deposited = AMOUNT;

        vm.warp(T0 + 300);
        vm.prank(beneficiary);
        vesting.claim(id); // 领走 300_000
        uint256 claimed = 300_000;

        token.mint(sender, 200_000);
        vm.prank(sender);
        vesting.topUp(id, 200_000); // 补足
        deposited += 200_000;

        vm.warp(T0 + 700);
        vm.prank(sender);
        vesting.cancel(id); // 退回未归属
        uint256 refunded = 1_200_000 - 1_200_000 * 700 / 1000;

        assertEq(token.balanceOf(address(vesting)), deposited - claimed - refunded, unicode"余额 = 存入 - 已领 - 已退");

        vm.prank(beneficiary);
        vesting.claim(id);
        assertEq(token.balanceOf(address(vesting)), 0, unicode"全部结清后合约余额为零");
    }

    // ------------------------------------------------------------------
    // 回调重入
    // ------------------------------------------------------------------

    function test_ReentrancyOnClaimIsBlocked() public {
        CallbackToken cbt = new CallbackToken();
        VestingStream v = new VestingStream(cbt);
        ReentrantClaimer attacker = new ReentrantClaimer(v);

        cbt.mint(sender, 1_000_000);
        vm.prank(sender);
        cbt.approve(address(v), type(uint256).max);

        vm.prank(sender);
        uint256 id = v.createStream(address(attacker), 1_000_000, T0, T0, T0 + 1000);
        attacker.setStreamId(id);

        vm.warp(T0 + 500);
        attacker.arm(ReentrantClaimer.Attack.ReenterClaim);
        cbt.setArmed(true);

        attacker.claim(); // 外层领取触发回调重入

        assertFalse(attacker.reentrySucceeded(), unicode"重入必须被拒绝");
        assertEq(
            bytes4(attacker.reentryRevertReason()),
            bytes4(keccak256("ReentrancyGuardReentrantCall()")),
            unicode"重入被 ReentrancyGuard 拦截"
        );
        // 外层领取正常完成，且只领取一次
        assertEq(cbt.balanceOf(address(attacker)), 500_000);
        assertEq(v.claimable(id), 0);
    }

    function test_ReentrancyOnCancelRefundIsBlocked() public {
        CallbackToken cbt = new CallbackToken();
        VestingStream v = new VestingStream(cbt);
        // 攻击者作为 sender：cancel 的退款转账会回调攻击者
        ReentrantClaimer attacker = new ReentrantClaimer(v);

        cbt.mint(address(attacker), 1_000_000);
        attacker.initToken(cbt);
        uint256 id = attacker.createStream(beneficiary, 1_000_000, T0, T0, T0 + 1000);
        attacker.setStreamId(id);

        vm.warp(T0 + 500);
        // 退款回调中依次尝试重入 cancel / claim / topUp
        ReentrantClaimer.Attack[3] memory attacks = [
            ReentrantClaimer.Attack.ReenterCancel,
            ReentrantClaimer.Attack.ReenterClaim,
            ReentrantClaimer.Attack.ReenterTopUp
        ];
        for (uint256 k = 0; k < attacks.length; k++) {
            // 每轮重新部署，保证流处于未撤销状态
            if (k > 0) {
                v = new VestingStream(cbt);
                attacker = new ReentrantClaimer(v);
                cbt.mint(address(attacker), 1_000_000);
                attacker.initToken(cbt);
                id = attacker.createStream(beneficiary, 1_000_000, T0, T0, T0 + 1000);
                attacker.setStreamId(id);
                vm.warp(T0 + 500);
            }
            attacker.arm(attacks[k]);
            cbt.setArmed(true);
            attacker.cancel();

            assertFalse(attacker.reentrySucceeded(), unicode"退款回调中的重入必须被拒绝");
            assertEq(
                bytes4(attacker.reentryRevertReason()),
                bytes4(keccak256("ReentrancyGuardReentrantCall()")),
                unicode"重入被 ReentrancyGuard 拦截"
            );
            // 外层 cancel 正常完成：未归属部分退回攻击者，受益人保留已归属额度
            assertEq(cbt.balanceOf(address(attacker)), 1_000_000 - 500_000);
            assertEq(v.claimable(id), 500_000);
            cbt.setArmed(false);
        }
    }
}

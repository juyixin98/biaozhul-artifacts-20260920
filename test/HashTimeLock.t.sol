// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {TestToken} from "../src/TestToken.sol";
import {MaliciousToken} from "./helpers/MaliciousToken.sol";
import {ReentrantAttacker} from "./helpers/ReentrantAttacker.sol";

/// @title HashTimeLock 单元测试
/// @dev 覆盖：正常锁定/领取/退款、重复领取、错误原像、时间恰好到期（claim 失败 / refund 成功）、
///      权限检查、零值检查、终态不可逆、重入攻击。
contract HashTimeLockTest is Test {
    HashTimeLock internal htlc;
    TestToken internal token;

    address internal alice = makeAddr("alice"); // 发送者
    address internal bob = makeAddr("bob"); // 接收者
    address internal carol = makeAddr("carol"); // 无关第三方

    bytes internal secret = hex"6d795f7365637265745f707265696d6167655f303031"; // "my_secret_preimage_001"
    bytes32 internal hashLock = sha256(secret);

    uint256 internal constant AMOUNT = 100 ether;
    uint64 internal constant T0 = 1_000_000;
    uint64 internal constant TIMEOUT = T0 + 3600;

    event Locked(
        uint256 indexed lockId,
        address indexed sender,
        address indexed receiver,
        address token,
        uint256 amount,
        bytes32 hashLock,
        uint64 timelock
    );
    event Claimed(uint256 indexed lockId, bytes secret);
    event Refunded(uint256 indexed lockId);

    function setUp() public {
        vm.warp(T0);
        htlc = new HashTimeLock();
        token = new TestToken("Test", "TST");

        token.mint(alice, AMOUNT);
        vm.prank(alice);
        token.approve(address(htlc), type(uint256).max);
    }

    function _lockAsAlice() internal returns (uint256 id) {
        vm.prank(alice);
        id = htlc.lock(bob, address(token), AMOUNT, hashLock, TIMEOUT);
    }

    // ------------------------------------------------------------------
    // 锁定
    // ------------------------------------------------------------------

    function test_LockStoresAllBindings() public {
        vm.expectEmit(true, true, true, true, address(htlc));
        emit Locked(1, alice, bob, address(token), AMOUNT, hashLock, TIMEOUT);

        uint256 id = _lockAsAlice();
        assertEq(id, 1, "first lock id must be 1");

        HashTimeLock.Lock memory l = htlc.getLock(id);
        assertEq(l.sender, alice);
        assertEq(l.receiver, bob);
        assertEq(l.token, address(token));
        assertEq(l.amount, AMOUNT);
        assertEq(l.hashLock, hashLock);
        assertEq(uint256(l.timelock), uint256(TIMEOUT));
        assertEq(uint8(l.state), uint8(HashTimeLock.State.LOCKED));
        assertEq(token.balanceOf(address(htlc)), AMOUNT, "custody holds funds");
        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.LOCKED));
    }

    function test_LockIdsAreMonotonic() public {
        uint256 a = _lockAsAlice();
        token.mint(alice, AMOUNT);
        uint256 b = _lockAsAlice();
        assertEq(b, a + 1);
    }

    function test_RevertWhen_LockToZeroReceiver() public {
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.ZeroAddress.selector);
        htlc.lock(address(0), address(token), AMOUNT, hashLock, TIMEOUT);
    }

    function test_RevertWhen_LockZeroAmount() public {
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.ZeroAmount.selector);
        htlc.lock(bob, address(token), 0, hashLock, TIMEOUT);
    }

    function test_RevertWhen_LockTimelockInPast() public {
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.TimelockInPast.selector);
        htlc.lock(bob, address(token), AMOUNT, hashLock, T0); // 等于当前时间即非法
    }

    // ------------------------------------------------------------------
    // 正常领取
    // ------------------------------------------------------------------

    function test_ClaimWithPreimageBeforeDeadline() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT - 1); // 截止前 1 秒，仍然有效

        vm.expectEmit(true, false, false, true, address(htlc));
        emit Claimed(id, secret);

        vm.prank(bob);
        htlc.claim(id, secret);

        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(token.balanceOf(bob), AMOUNT, "receiver gets funds");
        assertEq(token.balanceOf(address(htlc)), 0);
    }

    // ------------------------------------------------------------------
    // 重复领取
    // ------------------------------------------------------------------

    function test_RevertWhen_ClaimTwice() public {
        uint256 id = _lockAsAlice();
        vm.prank(bob);
        htlc.claim(id, secret);

        vm.prank(bob);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.claim(id, secret);

        assertEq(token.balanceOf(bob), AMOUNT, "paid exactly once");
    }

    function test_RevertWhen_SecondPartyClaimsAfterFirst() public {
        uint256 id = _lockAsAlice();
        vm.prank(bob);
        htlc.claim(id, secret);

        // 发送者自己也不能再动这笔已领取的锁
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.refund(id);
    }

    // ------------------------------------------------------------------
    // 错误原像 / 空原像
    // ------------------------------------------------------------------

    function test_RevertWhen_WrongPreimage() public {
        uint256 id = _lockAsAlice();
        bytes memory wrong = hex"00";
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.WrongSecret.selector);
        htlc.claim(id, wrong);
        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.LOCKED), "still locked");
    }

    function test_RevertWhen_EmptyPreimage() public {
        uint256 id = _lockAsAlice();
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.EmptySecret.selector);
        htlc.claim(id, hex"");
    }

    function test_RightSecretDifferentLengthIsWrong() public {
        // 原像不同，即使长度变化也必须被 sha256 摘要识别出来
        bytes memory tampered = bytes.concat(secret, hex"ff");
        uint256 id = _lockAsAlice();
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.WrongSecret.selector);
        htlc.claim(id, tampered);
    }

    // ------------------------------------------------------------------
    // 时间边界：恰好到期
    // ------------------------------------------------------------------

    /// @dev “恰好到期”：block.timestamp == timelock 时，claim 必须失败（严格 < 才允许领取）。
    function test_RevertWhen_ClaimExactlyAtDeadline() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT);
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.LockExpired.selector);
        htlc.claim(id, secret);
        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.LOCKED));
        assertEq(token.balanceOf(bob), 0);
    }

    function test_RevertWhen_ClaimAfterDeadline() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT + 123);
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.LockExpired.selector);
        htlc.claim(id, secret);
    }

    // ------------------------------------------------------------------
    // 退款
    // ------------------------------------------------------------------

    /// @dev “恰好到期”：block.timestamp == timelock 时，refund 必须成功（>= 即可退款）。
    function test_RefundExactlyAtDeadline() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT);

        vm.expectEmit(true, false, false, true, address(htlc));
        emit Refunded(id);

        vm.prank(alice);
        htlc.refund(id);

        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(token.balanceOf(alice), AMOUNT, "sender gets funds back");
    }

    function test_RevertWhen_RefundBeforeDeadline() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT - 1);
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.StillLocked.selector);
        htlc.refund(id);
    }

    function test_RevertWhen_RefundTwice() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT);
        vm.prank(alice);
        htlc.refund(id);
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.refund(id);
        assertEq(token.balanceOf(alice), AMOUNT, "refunded exactly once");
    }

    function test_RevertWhen_RefundedLockCannotBeClaimed() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT);
        vm.prank(alice);
        htlc.refund(id);

        vm.prank(bob);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.claim(id, secret);
    }

    // ------------------------------------------------------------------
    // 权限
    // ------------------------------------------------------------------

    function test_RevertWhen_NonReceiverClaims() public {
        uint256 id = _lockAsAlice();
        vm.prank(carol);
        vm.expectRevert(HashTimeLock.NotReceiver.selector);
        htlc.claim(id, secret);
    }

    function test_RevertWhen_NonSenderRefunds() public {
        uint256 id = _lockAsAlice();
        vm.warp(TIMEOUT);
        vm.prank(carol);
        vm.expectRevert(HashTimeLock.NotSender.selector);
        htlc.refund(id);
    }

    function test_RevertWhen_UnknownLockId() public {
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.UnknownLock.selector);
        htlc.claim(999, secret);

        vm.prank(alice);
        vm.expectRevert(HashTimeLock.UnknownLock.selector);
        htlc.refund(999);
    }

    // ------------------------------------------------------------------
    // 状态机：LOCKED 只能进入一种终态
    // ------------------------------------------------------------------

    function test_OnlyOneTerminalStateEver() public {
        // claim 路径：CLAIMED 后无法再 refund
        uint256 a = _lockAsAlice();
        vm.prank(bob);
        htlc.claim(a, secret);
        vm.warp(TIMEOUT + 9999);
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.refund(a);

        // refund 路径：REFUNDED 后无法再 claim（第二把锁使用更远的截止时间）
        token.mint(alice, AMOUNT);
        vm.prank(alice);
        uint256 b = htlc.lock(bob, address(token), AMOUNT, hashLock, TIMEOUT + 20_000);
        vm.warp(TIMEOUT + 20_000);
        vm.prank(alice);
        htlc.refund(b);
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.claim(b, secret);
    }

    // ------------------------------------------------------------------
    // 重入
    // ------------------------------------------------------------------

    function test_ClaimIsNotReentrant() public {
        MaliciousToken mal = new MaliciousToken();
        ReentrantAttacker attacker = new ReentrantAttacker(address(htlc));

        uint256 stake = 10 ether;
        mal.mint(alice, stake);
        vm.prank(alice);
        mal.approve(address(htlc), type(uint256).max);
        mal.setHook(address(attacker), true);

        vm.prank(alice);
        uint256 id = htlc.lock(address(attacker), address(mal), stake, hashLock, TIMEOUT);

        attacker.arm(id, secret);
        vm.prank(address(attacker));
        htlc.claim(id, secret);

        assertEq(attacker.reentrySuccesses(), 0, "reentrant claim must never succeed");
        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(mal.balanceOf(address(attacker)), stake, "paid exactly once");
        assertEq(mal.balanceOf(address(htlc)), 0);
    }

    function test_RefundIsNotReentrant() public {
        // 攻击合约同样可由 sender 部署并持有退款资金，验证 refund 的 nonReentrant
        MaliciousToken mal = new MaliciousToken();
        ReentrantAttacker attacker = new ReentrantAttacker(address(htlc));

        uint256 stake = 10 ether;
        mal.mint(address(attacker), stake);
        mal.setHook(address(attacker), true);

        vm.startPrank(address(attacker));
        mal.approve(address(htlc), type(uint256).max);
        uint256 id = htlc.lock(bob, address(mal), stake, hashLock, TIMEOUT);
        attacker.arm(id, secret);
        vm.stopPrank();

        vm.warp(TIMEOUT);
        // 退款打给 attacker 时触发回调；attacker 尝试重入 claim（已到期）与非重入保护都应失败
        vm.prank(address(attacker));
        htlc.refund(id);

        assertEq(attacker.reentrySuccesses(), 0);
        assertEq(uint8(htlc.getState(id)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(mal.balanceOf(address(attacker)), stake);
    }
}

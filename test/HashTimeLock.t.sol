// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {MockERC20} from "../src/MockERC20.sol";

/// @dev 零外部依赖：forge-std 由 Foundry 内置随版提供（见 README「依赖锁定」）。
contract HashTimeLockTest is Test {
    HashTimeLock htlc;
    MockERC20 token;

    address alice = makeAddr("alice"); // 锁定者 / 退款收款方
    address bob = makeAddr("bob"); // 领取者

    uint256 constant AMOUNT = 100 ether;
    // bytes32 原像：32 字节随机秘密
    bytes32 constant PREIMAGE = bytes32(keccak256("secret-preimage-0x42"));
    bytes32 HASHLOCK;

    event Locked(
        bytes32 indexed id,
        address indexed sender,
        address indexed receiver,
        address token,
        uint256 amount,
        bytes32 hashlock,
        uint256 timelock
    );
    event Claimed(bytes32 indexed id, bytes32 preimage);
    event Refunded(bytes32 indexed id);

    function setUp() public {
        htlc = new HashTimeLock();
        token = new MockERC20("Test Token", "TST");
        HASHLOCK = htlc.hashOf(PREIMAGE);
        token.mint(alice, 1_000 ether);
    }

    function _lock(address sender, address receiver, uint256 amount, bytes32 hashlock, uint256 ttl)
        internal
        returns (bytes32 id)
    {
        vm.prank(sender);
        token.approve(address(htlc), amount);
        vm.prank(sender);
        id = htlc.lock(receiver, address(token), amount, hashlock, block.timestamp + ttl);
    }

    // ---------- 基本锁定 ----------

    function test_Lock_EmitsEvent_AndEscrowsFunds() public {
        vm.prank(alice);
        token.approve(address(htlc), AMOUNT);
        uint256 tl = block.timestamp + 1 hours;
        // 与合约相同的 id 公式（首笔锁 nonce=0），用于精确比对事件
        bytes32 expectedId =
            keccak256(abi.encode(alice, bob, address(token), AMOUNT, HASHLOCK, tl, uint256(0), block.chainid));
        vm.expectEmit(true, true, true, true);
        emit Locked(expectedId, alice, bob, address(token), AMOUNT, HASHLOCK, tl);
        vm.prank(alice);
        bytes32 id = htlc.lock(bob, address(token), AMOUNT, HASHLOCK, tl);

        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.LOCKED));
        assertEq(token.balanceOf(address(htlc)), AMOUNT);

        HashTimeLock.Lock memory l = _get(id);
        assertEq(l.sender, alice);
        assertEq(l.receiver, bob);
        assertEq(l.token, address(token));
        assertEq(l.amount, AMOUNT);
        assertEq(l.hashlock, HASHLOCK);
        assertEq(l.timelock, tl);
    }

    function test_Lock_RevertOn_ZeroReceiver_ZeroAmount_PastTimelock() public {
        uint256 tl = block.timestamp + 1 hours;
        vm.prank(alice);
        token.approve(address(htlc), type(uint256).max);

        vm.prank(alice);
        vm.expectRevert(HashTimeLock.InvalidReceiver.selector);
        htlc.lock(address(0), address(token), AMOUNT, HASHLOCK, tl);

        vm.prank(alice);
        vm.expectRevert(HashTimeLock.InvalidAmount.selector);
        htlc.lock(bob, address(token), 0, HASHLOCK, tl);

        vm.prank(alice);
        vm.expectRevert(HashTimeLock.InvalidTimelock.selector);
        htlc.lock(bob, address(token), AMOUNT, HASHLOCK, block.timestamp);

        vm.prank(alice);
        vm.expectRevert(HashTimeLock.InvalidTimelock.selector);
        htlc.lock(bob, address(token), AMOUNT, HASHLOCK, block.timestamp - 1);
    }

    function test_Lock_DistinctIdsForIdenticalParams() public {
        bytes32 id1 = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        bytes32 id2 = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        assertTrue(id1 != id2, "nonce must distinguish identical locks");
    }

    // ---------- 领取 ----------

    function test_Claim_WithCorrectPreimage_BeforeDeadline() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        uint256 bobBefore = token.balanceOf(bob);

        vm.expectEmit(true, false, false, true);
        emit Claimed(id, PREIMAGE);
        // 任何人都能提交原像，但收款必须是绑定的 bob
        address relayer = makeAddr("relayer");
        vm.prank(relayer);
        htlc.claim(id, PREIMAGE);

        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(token.balanceOf(bob), bobBefore + AMOUNT, "receiver bound at lock must be paid");
        assertEq(token.balanceOf(address(htlc)), 0);
    }

    function test_Claim_RevertOn_WrongPreimage() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        bytes32 wrong = bytes32(uint256(PREIMAGE) ^ 1);

        vm.prank(bob);
        vm.expectRevert(HashTimeLock.WrongPreimage.selector);
        htlc.claim(id, wrong);

        // 失败后状态保持 LOCKED、资金未动
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.LOCKED));
        assertEq(token.balanceOf(address(htlc)), AMOUNT);
    }

    function test_Claim_RevertOn_NonexistentLock() public {
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.claim(bytes32("nope"), PREIMAGE);
    }

    function test_RepeatedClaim_Reverts_AfterSuccess() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        vm.prank(bob);
        htlc.claim(id, PREIMAGE);

        // 重复领取（同一原像再来一次）
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.claim(id, PREIMAGE);

        // 终态下也不能退款
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.refund(id);

        assertEq(token.balanceOf(bob), AMOUNT, "paid exactly once");
        assertEq(token.balanceOf(alice), 900 ether);
    }

    function test_Claim_ExactlyAtDeadline_Reverts_AndRefundSucceeds() public {
        // timelock 边界：block.timestamp == timelock 视为到期，领取失败、退款成功
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        HashTimeLock.Lock memory l = _get(id);

        vm.warp(l.timelock); // 恰好到期
        assertEq(block.timestamp, l.timelock);

        vm.expectRevert(HashTimeLock.TooLate.selector);
        htlc.claim(id, PREIMAGE);

        // 仍处 LOCKED，尚未进入终态
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.LOCKED));

        vm.expectEmit(true, false, false, true);
        emit Refunded(id);
        htlc.refund(id);
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(token.balanceOf(alice), 1_000 ether);
    }

    function test_Claim_OneSecondBeforeDeadline_Succeeds() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        HashTimeLock.Lock memory l = _get(id);
        vm.warp(l.timelock - 1);
        htlc.claim(id, PREIMAGE);
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.CLAIMED));
    }

    // ---------- 退款 ----------

    function test_Refund_BeforeDeadline_Reverts() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        vm.expectRevert(HashTimeLock.TooEarly.selector);
        htlc.refund(id);
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.LOCKED));
    }

    function test_Refund_AfterDeadline_ReturnsFundsToSender() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        HashTimeLock.Lock memory l = _get(id);
        vm.warp(l.timelock + 1);

        address anyone = makeAddr("anyone");
        vm.prank(anyone); // 任何人可触发，但钱只回 sender
        htlc.refund(id);

        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(token.balanceOf(alice), 1_000 ether);
    }

    function test_RepeatedRefund_Reverts() public {
        bytes32 id = _lock(alice, bob, AMOUNT, HASHLOCK, 1 hours);
        HashTimeLock.Lock memory l = _get(id);
        vm.warp(l.timelock + 1);
        htlc.refund(id);

        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.refund(id);

        // 终态下不能再领取
        vm.expectRevert(HashTimeLock.NotLocked.selector);
        htlc.claim(id, PREIMAGE);
    }

    function test_LockPull_Fails_WhenNotApproved() public {
        vm.prank(alice);
        vm.expectRevert(bytes("ERC20: insufficient allowance"));
        htlc.lock(bob, address(token), AMOUNT, HASHLOCK, block.timestamp + 1 hours);
    }

    function _get(bytes32 id) internal view returns (HashTimeLock.Lock memory l) {
        (address sender, address receiver, address t, uint256 amount, bytes32 h, uint256 tl, HashTimeLock.State st) =
            htlc.locks(id);
        l = HashTimeLock.Lock(sender, receiver, t, amount, h, tl, st);
    }
}

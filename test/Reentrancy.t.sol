// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {ReentrantERC20} from "./helpers/ReentrantERC20.sol";
import {AttackerReceiver} from "./helpers/AttackerReceiver.sol";
import {UnsafeHTLC} from "./helpers/UnsafeHTLC.sol";

/// @title ReentrancyTest —— 真实执行重入攻击，验证安全合约挡住攻击、脆弱合约被攻破
contract ReentrancyTest is Test {
    HashTimeLock htlc;
    ReentrantERC20 token;
    AttackerReceiver attacker;

    address victim = makeAddr("victim"); // 锁定者
    uint256 constant AMOUNT = 100 ether;
    bytes32 constant PREIMAGE = bytes32(keccak256("reentrancy-secret"));
    bytes32 HASHLOCK;

    function setUp() public {
        htlc = new HashTimeLock();
        token = new ReentrantERC20();
        attacker = new AttackerReceiver();
        HASHLOCK = keccak256(abi.encodePacked(PREIMAGE));

        token.mint(victim, 10_000 ether);
        token.mint(address(attacker), 1_000 ether);
    }

    /// 攻击 1：受害者锁定给攻击者合约地址，攻击者在 claim 收款回调里再次 claim 同一把锁。
    function test_SafeContract_BlocksClaimReentrancy() public {
        uint256 tl = block.timestamp + 1 hours;
        vm.startPrank(victim);
        token.approve(address(htlc), AMOUNT);
        bytes32 id = htlc.lock(address(attacker), address(token), AMOUNT, HASHLOCK, tl);
        vm.stopPrank();

        attacker.configure(address(htlc), address(token), id, PREIMAGE, AttackerReceiver.Mode.CLAIM);

        // 外层 claim 进行到转账时触发回调，回调里的重入 claim 被互斥锁拒绝，整笔回滚
        vm.expectRevert(HashTimeLock.ReentrantCall.selector);
        attacker.startClaim();

        // 攻击整体回滚：锁仍是 LOCKED，资金仍在合约里，攻击者一分钱没拿到
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.LOCKED));
        assertEq(token.balanceOf(address(htlc)), AMOUNT);
        assertEq(token.balanceOf(address(attacker)), 1_000 ether);

        // 关闭攻击后，攻击者合约作为绑定收款方可正常领取一次
        attacker.configure(address(htlc), address(token), id, PREIMAGE, AttackerReceiver.Mode.OFF);
        attacker.startClaim();
        assertEq(uint8(htlc.stateOf(id)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(token.balanceOf(address(attacker)), 1_000 ether + AMOUNT);
    }

    /// 攻击 2：攻击者自己锁两笔（合约持有 2*AMOUNT）。外层退 id1 的收款回调里重入退 id2。
    function test_SafeContract_BlocksRefundReentrancy() public {
        uint256 tl = block.timestamp + 1 hours;
        attacker.configure(address(htlc), address(token), bytes32(0), PREIMAGE, AttackerReceiver.Mode.OFF);
        (bytes32 id1, bytes32 id2) = attacker.seedTwoLocks(makeAddr("receiver"), AMOUNT, HASHLOCK, tl);
        assertEq(token.balanceOf(address(htlc)), 2 * AMOUNT);

        vm.warp(tl);
        // 回调中重入的目标是 id2
        attacker.configure(address(htlc), address(token), id2, PREIMAGE, AttackerReceiver.Mode.REFUND);

        // 外层由攻击者合约直接触发 id1 的退款；其收款回调重入 id2
        vm.prank(address(attacker));
        vm.expectRevert(HashTimeLock.ReentrantCall.selector);
        htlc.refund(id1);

        // 整体回滚：两笔都还在，攻击者此前锁定的 200 仍被托管（余额 1000-200）
        assertEq(uint8(htlc.stateOf(id1)), uint8(HashTimeLock.State.LOCKED));
        assertEq(uint8(htlc.stateOf(id2)), uint8(HashTimeLock.State.LOCKED));
        assertEq(token.balanceOf(address(htlc)), 2 * AMOUNT);
        assertEq(token.balanceOf(address(attacker)), 800 ether);

        // 关掉攻击后两笔可逐笔正常退款
        attacker.configure(address(htlc), address(token), id1, PREIMAGE, AttackerReceiver.Mode.OFF);
        attacker.startRefund();
        attacker.configure(address(htlc), address(token), id2, PREIMAGE, AttackerReceiver.Mode.OFF);
        attacker.startRefund();
        assertEq(uint8(htlc.stateOf(id1)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(uint8(htlc.stateOf(id2)), uint8(HashTimeLock.State.REFUNDED));
        // 攻击者拿回自己锁定的 200，不多不少
        assertEq(token.balanceOf(address(attacker)), 1_000 ether);
        assertEq(token.balanceOf(address(htlc)), 0);
    }

    /// 对照实验：无互斥锁、违反 CEI 的 UnsafeHTLC 被同一套攻击真实攻破。
    /// 攻击者用 id1 一把锁，在收款回调里连续重入 claim，共转走 4 笔款，
    /// id2~id4 的锁仍显示 LOCKED 却已无资金可领。
    function test_UnsafeContract_IsActuallyDrained() public {
        UnsafeHTLC unsafeHtlc = new UnsafeHTLC();

        uint256 tl = block.timestamp + 1 hours;
        bytes32[4] memory ids;
        vm.startPrank(victim);
        token.approve(address(unsafeHtlc), type(uint256).max);
        for (uint256 i = 0; i < 4; i++) {
            ids[i] = unsafeHtlc.lock(address(attacker), address(token), AMOUNT, HASHLOCK, tl);
        }
        vm.stopPrank();
        assertEq(token.balanceOf(address(unsafeHtlc)), 4 * AMOUNT);

        // 外层 1 次 + 回调重入 3 次，全部针对 id1
        attacker.configure(address(unsafeHtlc), address(token), ids[0], PREIMAGE, AttackerReceiver.Mode.CLAIM);
        attacker.setMaxDepth(3);
        attacker.startClaim();

        uint256 gained = token.balanceOf(address(attacker)) - 1_000 ether;
        console2.log("attacker gained by reentering one lock (wei):", gained);
        assertEq(gained, 4 * AMOUNT, "reentrancy must demonstrably drain the unsafe contract");
        assertEq(token.balanceOf(address(unsafeHtlc)), 0, "all escrowed funds stolen");

        // id1 帧展开后被置为 CLAIMED；但 id2~id4 永远停在 LOCKED，背后已无资金
        assertEq(uint8(unsafeHtlc.stateOf(ids[0])), uint8(UnsafeHTLC.State.CLAIMED));
        for (uint256 i = 1; i < 4; i++) {
            assertEq(uint8(unsafeHtlc.stateOf(ids[i])), uint8(UnsafeHTLC.State.LOCKED));
        }
    }
}

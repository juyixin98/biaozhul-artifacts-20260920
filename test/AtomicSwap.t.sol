// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, Vm} from "forge-std/Test.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {TestToken} from "../src/TestToken.sol";

/// @title 双链原子兑换模拟
/// @notice 用两个 HashTimeLock 实例模拟两条独立本地区块链：
///         链 A：Alice 用 100 TKA 锁定给 Bob；链 B：Bob 用 50 TKB 锁定给 Alice。
///         两把锁共享同一个 sha256 原像。
///
///         安全时间间隔：TA(A 链截止) = TB(B 链截止) + MARGIN。
///         Alice 在 B 上领取会暴露原像，Bob 必须在 A 到期前拿着原像领取，
///         多出来的 MARGIN 就是给 Bob 的反应窗口。
contract AtomicSwapTest is Test {
    HashTimeLock internal htlcA; // 模拟链 A
    HashTimeLock internal htlcB; // 模拟链 B
    TestToken internal tkA;
    TestToken internal tkB;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    bytes internal secret = hex"63726f7373636861696e5f7365637265745f303037"; // "crosschain_secret_007"
    bytes32 internal hashLock = sha256(secret);

    uint256 internal constant AMOUNT_A = 100 ether; // Alice 卖出 100 TKA
    uint256 internal constant AMOUNT_B = 50 ether; // 换得 50 TKB

    uint64 internal constant T0 = 1_000_000;
    uint64 internal constant TB = T0 + 200; // B 链（被领方）更早到期
    uint64 internal constant MARGIN = 400; // 明确安全间隔（秒）
    uint64 internal constant TA = TB + MARGIN; // A 链更晚到期

    /// @dev keccak256("Claimed(uint256,bytes)") —— 原像就在该日志的 data 区。
    bytes32 internal constant CLAIMED_TOPIC = keccak256("Claimed(uint256,bytes)");

    function setUp() public {
        vm.warp(T0);
        htlcA = new HashTimeLock();
        htlcB = new HashTimeLock();
        tkA = new TestToken("Token A", "TKA");
        tkB = new TestToken("Token B", "TKB");

        tkA.mint(alice, AMOUNT_A * 3);
        tkB.mint(bob, AMOUNT_B * 3);
        vm.prank(alice);
        tkA.approve(address(htlcA), type(uint256).max);
        vm.prank(bob);
        tkB.approve(address(htlcB), type(uint256).max);
    }

    /// @dev 从收据日志中提取 Claimed 事件公开的原像（真实脚本里对端就是这样拿到 secret 的）。
    function _extractSecret(Vm.Log[] memory logs) internal pure returns (bytes memory preimage) {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == CLAIMED_TOPIC) {
                preimage = abi.decode(logs[i].data, (bytes));
                return preimage;
            }
        }
        revert("no Claimed event found in logs");
    }

    // ------------------------------------------------------------------
    // 场景 1：正常兑换——一边领取暴露原像，另一边用同一原像领取
    // ------------------------------------------------------------------

    function test_FullAtomicSwap_HappyPath() public {
        // 1) Alice 在 A 链锁定 100 TKA 给 Bob（长超时）
        vm.prank(alice);
        uint256 idA = htlcA.lock(bob, address(tkA), AMOUNT_A, hashLock, TA);

        // 2) Bob 核对 hashLock 与金额后，在 B 链锁定 50 TKB 给 Alice（短超时）
        vm.prank(bob);
        uint256 idB = htlcB.lock(alice, address(tkB), AMOUNT_B, hashLock, TB);

        assertEq(uint8(htlcA.getState(idA)), uint8(HashTimeLock.State.LOCKED));
        assertEq(uint8(htlcB.getState(idB)), uint8(HashTimeLock.State.LOCKED));

        // 3) Alice 在 B 链到期前出示原像领取 50 TKB —— 原像随即在日志中公开
        vm.warp(T0 + 100);
        vm.recordLogs();
        vm.prank(alice);
        htlcB.claim(idB, secret);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes memory extracted = _extractSecret(logs);
        assertEq(extracted, secret, "preimage recovered from receipt must match");
        assertEq(sha256(extracted), hashLock);

        // 4) Bob（任何人都可读到链上日志）用拿到的原像在 A 链领取 100 TKA
        vm.warp(T0 + 140);
        vm.prank(bob);
        htlcA.claim(idA, extracted);

        assertEq(uint8(htlcB.getState(idB)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(uint8(htlcA.getState(idA)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(tkB.balanceOf(alice), AMOUNT_B, "alice received TKB");
        assertEq(tkA.balanceOf(bob), AMOUNT_A, "bob received TKA");
    }

    // ------------------------------------------------------------------
    // 场景 2：未完成兑换——两边各自到期退款
    // ------------------------------------------------------------------

    function test_AbortedSwap_BothSidesRefund() public {
        // Alice 先锁，Bob 也锁了，但 Alice 始终不领取（协商破裂 / 离线）
        vm.prank(alice);
        uint256 idA = htlcA.lock(bob, address(tkA), AMOUNT_A, hashLock, TA);
        vm.prank(bob);
        uint256 idB = htlcB.lock(alice, address(tkB), AMOUNT_B, hashLock, TB);

        // 到期前双方都不能退款
        vm.warp(TB - 1);
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.StillLocked.selector);
        htlcB.refund(idB);
        vm.prank(alice);
        vm.expectRevert(HashTimeLock.StillLocked.selector);
        htlcA.refund(idA);

        // B 链先到期，Bob 先退
        vm.warp(TB);
        vm.prank(bob);
        htlcB.refund(idB);
        assertEq(tkB.balanceOf(bob), AMOUNT_B * 3, "bob refunded on B");

        // A 链后到期，Alice 后退
        vm.warp(TA);
        vm.prank(alice);
        htlcA.refund(idA);
        assertEq(tkA.balanceOf(alice), AMOUNT_A * 3, "alice refunded on A");

        // 原像从未上链，谁也无法再领取
        assertEq(uint8(htlcA.getState(idA)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(uint8(htlcB.getState(idB)), uint8(HashTimeLock.State.REFUNDED));
    }

    // ------------------------------------------------------------------
    // 场景 3：安全间隔生效——B 链最后一刻领取，Bob 仍有 MARGIN 窗口在 A 链领取
    // ------------------------------------------------------------------

    function test_SafetyMargin_GivesBobWindow() public {
        vm.prank(alice);
        uint256 idA = htlcA.lock(bob, address(tkA), AMOUNT_A, hashLock, TA);
        vm.prank(bob);
        uint256 idB = htlcB.lock(alice, address(tkB), AMOUNT_B, hashLock, TB);

        // Alice 卡着 B 链到期前 1 秒才领取（对 Bob 最不利的时机）
        vm.warp(TB - 1);
        vm.recordLogs();
        vm.prank(alice);
        htlcB.claim(idB, secret);
        bytes memory extracted = _extractSecret(vm.getRecordedLogs());

        // 即便如此，Bob 在 A 链仍有整整 MARGIN 秒；最后 1 秒领取成功
        vm.warp(TA - 1);
        vm.prank(bob);
        htlcA.claim(idA, extracted);

        assertEq(uint8(htlcA.getState(idA)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(tkA.balanceOf(bob), AMOUNT_A);
    }

    /// @dev 反面教材：Bob 若拖过 A 链截止时间，A 侧资金被 Alice 退款，
    ///      而 B 侧原像已暴露、Alice 已得款——这正是必须设置安全间隔的原因。
    function test_SafetyMarginExhausted_BobMissesA() public {
        vm.prank(alice);
        uint256 idA = htlcA.lock(bob, address(tkA), AMOUNT_A, hashLock, TA);
        vm.prank(bob);
        uint256 idB = htlcB.lock(alice, address(tkB), AMOUNT_B, hashLock, TB);

        vm.warp(TB - 1);
        vm.recordLogs();
        vm.prank(alice);
        htlcB.claim(idB, secret);
        bytes memory extracted = _extractSecret(vm.getRecordedLogs());

        // Bob 拖延到 A 链恰好到期：claim 失败
        vm.warp(TA);
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.LockExpired.selector);
        htlcA.claim(idA, extracted);

        // Alice 退款成功——Bob 资金两空（仅 A 侧这笔演示）
        vm.prank(alice);
        htlcA.refund(idA);
        assertEq(uint8(htlcA.getState(idA)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(uint8(htlcB.getState(idB)), uint8(HashTimeLock.State.CLAIMED));
    }
}

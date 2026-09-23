// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {HashTimeLock} from "../src/HashTimeLock.sol";
import {MockERC20} from "../src/MockERC20.sol";

/// @title AtomicSwapTest —— 两条本地链的哈希时间锁兑换（在单一 EVM 测试进程中模拟两条链）
/// @notice 角色与资产流（经典 Alice/Bob 原子兑换，两条腿共用同一原像）：
///
///   链 A：Alice 锁定 100 TKN-A，接收者=Bob            链 B：Bob 锁定 100 TKN-B，接收者=Alice
///   期限 tA（较短，例如 1h）                            期限 tB = tA + SAFETY_GAP（更长，例如 2h）
///
///   1) Bob 仅在 tA 到期前能在 A 上领取（若不领取，Alice 到期退款，Bob 不会再锁 B）；
///   2) Bob 在 A 上领取时原像上链公开；Alice 取得原像后在 B 上领取；
///   3) 任一方不配合，到期各自退款，谁也拿不到对方的资产。
contract AtomicSwapTest is Test {
    /// 安全间隔：后手腿（B）的时间锁必须比先手腿（A）长出明确的量，
    /// 让 Alice 有充足时间在看到原像后领取 B，且 B 的退款窗口晚于 A。
    uint256 constant SAFETY_GAP = 1 hours;
    uint256 constant TTL_A = 1 hours;
    uint256 constant TTL_B = TTL_A + SAFETY_GAP; // 2h
    uint256 constant AMOUNT_A = 100 ether;
    uint256 constant AMOUNT_B = 100 ether;

    HashTimeLock htlcA;
    HashTimeLock htlcB;
    MockERC20 tokenA;
    MockERC20 tokenB;

    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    bytes32 preimage;
    bytes32 hashlock;
    bytes32 idA;
    bytes32 idB;

    function setUp() public {
        htlcA = new HashTimeLock();
        htlcB = new HashTimeLock();
        tokenA = new MockERC20("Token A", "TKNA");
        tokenB = new MockERC20("Token B", "TKNB");

        tokenA.mint(alice, 1_000 ether);
        tokenB.mint(bob, 1_000 ether);

        // 真实生成 32 字节密码学随机原像，并计算 keccak256 摘要（模拟秘密生成方 Alice 的操作）
        preimage = keccak256(abi.encodePacked("atomic-swap-secret", block.prevrandao, block.number));
        hashlock = htlcA.hashOf(preimage);
        assertEq(hashlock, keccak256(abi.encodePacked(preimage)));
        console2.log("preimage :");
        console2.logBytes32(preimage);
        console2.log("hashlock :");
        console2.logBytes32(hashlock);
    }

    function _aliceLockA() internal returns (bytes32 id) {
        vm.startPrank(alice);
        tokenA.approve(address(htlcA), AMOUNT_A);
        id = htlcA.lock(bob, address(tokenA), AMOUNT_A, hashlock, block.timestamp + TTL_A);
        vm.stopPrank();
    }

    function _bobLockB() internal returns (bytes32 id) {
        vm.startPrank(bob);
        tokenB.approve(address(htlcB), AMOUNT_B);
        id = htlcB.lock(alice, address(tokenB), AMOUNT_B, hashlock, block.timestamp + TTL_B);
        vm.stopPrank();
    }

    /// 场景 1：快乐路径 —— Bob 在 A 领取暴露原像，Alice 用同一原像在 B 领取。
    function test_Swap_HappyPath_ClaimThenRevealedPreimage() public {
        idA = _aliceLockA();
        idB = _bobLockB();
        assertEq(tokenA.balanceOf(address(htlcA)), AMOUNT_A);
        assertEq(tokenB.balanceOf(address(htlcB)), AMOUNT_B);

        // —— 腿 1：Bob 在链 A 截止前领取，原像随 Claimed 事件上链 ——
        uint256 revealAt = block.timestamp + 30 minutes;
        vm.warp(revealAt);
        vm.prank(bob);
        htlcA.claim(idA, preimage);
        assertEq(tokenA.balanceOf(bob), AMOUNT_A);
        assertEq(uint8(htlcA.stateOf(idA)), uint8(HashTimeLock.State.CLAIMED));

        // —— Alice（或任何观察者）从链 A 事件读取原像，并在链 B 截止前使用 ——
        //    （真实部署中通过事件日志/收据取得；此处直接使用同一 preimage 变量模拟读取结果）
        bytes32 revealed = preimage;
        assertEq(keccak256(abi.encodePacked(revealed)), hashlock);

        uint256 aliceClaimAt = revealAt + 5 minutes;
        vm.warp(aliceClaimAt);
        vm.prank(alice);
        htlcB.claim(idB, revealed);

        // 终态核对
        assertEq(tokenB.balanceOf(alice), AMOUNT_B);
        assertEq(uint8(htlcB.stateOf(idB)), uint8(HashTimeLock.State.CLAIMED));
        assertEq(tokenA.balanceOf(alice), 900 ether);
        assertEq(tokenB.balanceOf(bob), 900 ether);
    }

    /// 场景 2：Bob 始终不在 A 领取 -> A 到期 Alice 退款；B 从未锁定（协议顺序保证），
    ///         或即便已锁定，B 到期后 Bob 也只能退款。
    function test_Swap_NoParticipation_BothSidesRefund() public {
        idA = _aliceLockA();
        idB = _bobLockB(); // 模拟双方都已上锁但无人领取

        // tA 到期：Alice 在 A 上退款
        vm.warp(block.timestamp + TTL_A);
        htlcA.refund(idA);
        assertEq(uint8(htlcA.stateOf(idA)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(tokenA.balanceOf(alice), 1_000 ether);

        // tA 已到期但 tB 还没到（安全间隔内）：Bob 在 B 上不能提前退款
        vm.expectRevert(HashTimeLock.TooEarly.selector);
        htlcB.refund(idB);

        // 原像从未公开，Alice 无法领取 B
        vm.expectRevert(HashTimeLock.WrongPreimage.selector);
        htlcB.claim(idB, bytes32("guessed-secret"));

        // tB 到期（再过一个 SAFETY_GAP）：Bob 在 B 上退款，双方各自拿回资产
        vm.warp(block.timestamp + SAFETY_GAP);
        htlcB.refund(idB);
        assertEq(uint8(htlcB.stateOf(idB)), uint8(HashTimeLock.State.REFUNDED));
        assertEq(tokenB.balanceOf(bob), 1_000 ether);
    }

    /// 场景 3：安全间隔的必要性 —— Bob 在 A 到期前最后一刻领取并暴露原像，
    ///         Alice 仍有完整 SAFETY_GAP 时间在 B 上领取，B 不可能已到期。
    function test_Swap_SafetyGap_LastMinuteRevealStillClaimable() public {
        idA = _aliceLockA();
        idB = _bobLockB();

        // Bob 在 A 到期前 1 秒领取（最坏情况）
        vm.warp(block.timestamp + TTL_A - 1);
        htlcA.claim(idA, preimage);

        // 此时距 B 到期仍有 SAFETY_GAP + 1 秒
        (,,,,, uint256 tlB,) = htlcB.locks(idB);
        assertGe(tlB - block.timestamp, SAFETY_GAP + 1);

        // Alice 用掉整个间隔后仍可领取（间隔留 1 秒）
        vm.warp(block.timestamp + SAFETY_GAP);
        assertLt(block.timestamp, tlB);
        htlcB.claim(idB, preimage);
        assertEq(uint8(htlcB.stateOf(idB)), uint8(HashTimeLock.State.CLAIMED));
    }

    /// 场景 4：错误原像不能领取 A；且 A 到期后即使拿到正确原像也不能再领取。
    function test_Swap_WrongOrLatePreimage_DoesNotClaim() public {
        idA = _aliceLockA();
        bytes32 wrong = bytes32(uint256(preimage) ^ uint256(1));

        vm.prank(bob);
        vm.expectRevert(HashTimeLock.WrongPreimage.selector);
        htlcA.claim(idA, wrong);

        // 到期
        vm.warp(block.timestamp + TTL_A);
        vm.prank(bob);
        vm.expectRevert(HashTimeLock.TooLate.selector);
        htlcA.claim(idA, preimage);

        htlcA.refund(idA);
        assertEq(uint8(htlcA.stateOf(idA)), uint8(HashTimeLock.State.REFUNDED));
    }
}

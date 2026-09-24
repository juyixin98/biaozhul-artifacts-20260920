// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HTLC} from "../contracts/HTLC.sol";

// 最小 cheatcode 接口：本项目刻意不依赖 forge-std，只声明用到的部分。
interface Vm {
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function warp(uint256) external;
    function expectRevert() external;
    function expectRevert(bytes calldata) external;
    function addr(uint256) external returns (address);
    function deal(address, uint256) external;
}

contract HTLCTest {
    Vm constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    HTLC internal htlc;
    address internal sender;
    address internal receiver;
    bytes32 internal constant PREIMAGE = bytes32(uint256(0x1234));
    bytes32 internal hashLock;
    bytes32 internal id = keccak256("swap-1");
    uint64 internal constant TIMELOCK = 1_000_000;
    uint256 internal constant AMOUNT = 1 ether;

    function setUp() public {
        htlc = new HTLC();
        sender = vm.addr(1);
        receiver = vm.addr(2);
        vm.deal(sender, 10 ether);
        hashLock = keccak256(abi.encodePacked(PREIMAGE));
    }

    function _lock() internal {
        vm.prank(sender);
        htlc.lock{value: AMOUNT}(id, receiver, hashLock, TIMELOCK);
    }

    // ---------- 正常流程 ----------

    function test_Lock_StoresStateAndEmits() public {
        _lock();
        (address s, address r, uint256 amt, bytes32 h, uint64 tl, HTLC.State st) = htlc.getSwap(id);
        require(s == sender, "sender");
        require(r == receiver, "receiver");
        require(amt == AMOUNT, "amount");
        require(h == hashLock, "hash");
        require(tl == TIMELOCK, "timelock");
        require(st == HTLC.State.Locked, "state");
        require(address(htlc).balance == AMOUNT, "contract balance");
    }

    function test_Claim_WithCorrectPreimage_PaysReceiver() public {
        _lock();
        vm.prank(receiver);
        htlc.claim(id, PREIMAGE);
        require(receiver.balance == AMOUNT, "receiver paid");
        require(htlc.stateOf(id) == HTLC.State.Claimed, "claimed");
        require(address(htlc).balance == 0, "emptied");
    }

    // ---------- 领取：错误原像 / 错误身份 / 重复 ----------

    function test_Claim_WrongPreimage_Reverts() public {
        _lock();
        vm.prank(receiver);
        vm.expectRevert();
        htlc.claim(id, bytes32(uint256(0xDEAD)));
        require(htlc.stateOf(id) == HTLC.State.Locked, "must stay locked");
    }

    function test_Claim_ByNonReceiver_Reverts() public {
        _lock();
        vm.prank(sender);
        vm.expectRevert();
        htlc.claim(id, PREIMAGE);
    }

    function test_Claim_Twice_SecondReverts() public {
        _lock();
        vm.prank(receiver);
        htlc.claim(id, PREIMAGE);
        vm.prank(receiver);
        vm.expectRevert();
        htlc.claim(id, PREIMAGE);
        require(receiver.balance == AMOUNT, "paid exactly once");
    }

    // ---------- 退款：截止时刻边界 ----------

    function test_Refund_BeforeTimelock_Reverts() public {
        _lock();
        vm.warp(TIMELOCK - 1); // 截止前 1 秒：不能退
        vm.prank(sender);
        vm.expectRevert();
        htlc.refund(id);
        require(htlc.stateOf(id) == HTLC.State.Locked, "must stay locked");
    }

    function test_Refund_ExactlyAtTimelock_Succeeds() public {
        _lock();
        vm.warp(TIMELOCK); // 边界：block.timestamp == timelock，条件 >= 成立
        uint256 beforeBal = sender.balance;
        htlc.refund(id);
        require(sender.balance == beforeBal + AMOUNT, "sender refunded");
        require(htlc.stateOf(id) == HTLC.State.Refunded, "refunded");
    }

    function test_Refund_AfterTimelock_Succeeds() public {
        _lock();
        vm.warp(TIMELOCK + 999);
        htlc.refund(id);
        require(htlc.stateOf(id) == HTLC.State.Refunded, "refunded");
    }

    // ---------- 领取 / 退款互斥（含终态重复操作） ----------

    function test_ClaimThenRefund_Reverts() public {
        _lock();
        vm.prank(receiver);
        htlc.claim(id, PREIMAGE);
        vm.warp(TIMELOCK + 1);
        vm.expectRevert();
        htlc.refund(id);
        require(htlc.stateOf(id) == HTLC.State.Claimed, "stays claimed");
    }

    function test_RefundThenClaim_RevertsEvenWithPreimage() public {
        _lock();
        vm.warp(TIMELOCK);
        htlc.refund(id);
        vm.prank(receiver);
        vm.expectRevert(); // 即使手握正确原像也无法领取
        htlc.claim(id, PREIMAGE);
        require(htlc.stateOf(id) == HTLC.State.Refunded, "stays refunded");
        require(receiver.balance == 0, "receiver got nothing");
    }

    function test_Refund_Twice_SecondReverts() public {
        _lock();
        vm.warp(TIMELOCK);
        htlc.refund(id);
        vm.expectRevert();
        htlc.refund(id);
        require(sender.balance == 10 ether, "refunded exactly once");
    }

    // ---------- 锁定规则 ----------

    function test_Lock_SameIdTwice_Reverts() public {
        _lock();
        vm.prank(sender);
        vm.expectRevert();
        htlc.lock{value: AMOUNT}(id, receiver, hashLock, TIMELOCK);
    }

    function test_Lock_ZeroValue_Reverts() public {
        vm.prank(sender);
        vm.expectRevert();
        htlc.lock(keccak256("x"), receiver, hashLock, TIMELOCK);
    }

    function test_UnknownSwap_Reverts() public {
        vm.expectRevert();
        htlc.claim(keccak256("nope"), PREIMAGE);
        vm.expectRevert();
        htlc.refund(keccak256("nope"));
    }

    // ---------- CEI：领取转币时的重入不能改变终态 ----------

    function test_Claim_ReentrancyCannotSettleTwice() public {
        ReentrancyAttacker attacker = new ReentrancyAttacker(htlc);
        vm.deal(sender, 10 ether);
        vm.prank(sender);
        htlc.lock{value: AMOUNT}(keccak256("atk"), address(attacker), hashLock, TIMELOCK);
        attacker.attack(keccak256("atk"), PREIMAGE);
        require(htlc.stateOf(keccak256("atk")) == HTLC.State.Claimed, "claimed");
        require(address(attacker).balance == AMOUNT, "attacker paid exactly once");
        require(address(htlc).balance == 0, "no leftover");
    }
}

/// @dev 领取收款时尝试重入"同一笔" claim；因状态已先置为 Claimed，
///      重入必然 AlreadySettled。用底层 call 验证返回 false，
///      且重入不能把合约余额转走第二次。
contract ReentrancyAttacker {
    HTLC internal immutable htlc;
    bytes32 internal currentId;

    constructor(HTLC _htlc) {
        htlc = _htlc;
    }

    function attack(bytes32 id, bytes32 preimage) external {
        currentId = id;
        htlc.claim(id, preimage);
    }

    receive() external payable {
        (bool ok,) = address(htlc).call(
            abi.encodeWithSelector(HTLC.claim.selector, currentId, bytes32(uint256(0x1234)))
        );
        require(!ok, "reentrant claim on the same swap must fail");
    }
}

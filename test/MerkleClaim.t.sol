// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {MerkleClaim} from "../src/MerkleClaim.sol";
import {TestMerkle} from "./TestMerkle.sol";
import {RejectReceiver} from "./RejectReceiver.sol";

/// @dev 验收测试：跨合约重放、重复索引、批次单项无效、转账失败原子性。
contract MerkleClaimTest is Test {
    MerkleClaim internal claimA;
    MerkleClaim internal claimB;
    RejectReceiver internal rejecter;

    address internal deployer = makeAddr("deployer");
    address internal executor = makeAddr("executor");

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");
    address internal dave = makeAddr("dave");
    address internal erin = makeAddr("erin");
    address internal frank = makeAddr("frank");
    address internal grace = makeAddr("grace");

    uint256 internal constant N = 8;

    function _accounts() internal view returns (address[N] memory a) {
        a[0] = alice;
        a[1] = bob;
        a[2] = carol;
        a[3] = address(rejecter);
        a[4] = dave;
        a[5] = erin;
        a[6] = frank;
        a[7] = grace;
    }

    function _amounts() internal pure returns (uint256[N] memory m) {
        m[0] = 1 ether;
        m[1] = 2 ether;
        m[2] = 3 ether;
        m[3] = 0.5 ether;
        m[4] = 4 ether;
        m[5] = 5 ether;
        m[6] = 6 ether;
        m[7] = 7 ether;
    }

    function _totalFunds() internal pure returns (uint256 total) {
        uint256[N] memory m = _amounts();
        for (uint256 i = 0; i < N; i++) total += m[i];
    }

    function _buildLeaves(address claimContract, uint256 chainId)
        internal
        view
        returns (bytes32[N] memory leaves)
    {
        address[N] memory a = _accounts();
        uint256[N] memory m = _amounts();
        for (uint256 i = 0; i < N; i++) {
            leaves[i] = TestMerkle.hashLeaf(chainId, claimContract, i, a[i], m[i]);
        }
    }

    /// @dev 叶子数组在 Solidity 内存中是定长数组，需要转成动态数组供 TestMerkle 使用。
    function _dyn(bytes32[N] memory fixedArr)
        internal
        pure
        returns (bytes32[] memory d)
    {
        d = new bytes32[](N);
        for (uint256 i = 0; i < N; i++) d[i] = fixedArr[i];
    }

    function _proof(bytes32[] memory leaves, uint256 index)
        internal
        pure
        returns (bytes32[] memory)
    {
        return TestMerkle.getProof(leaves, index);
    }

    function setUp() public {
        // rejecter 由测试合约自己部署，不占用 deployer 的 nonce。
        rejecter = new RejectReceiver();

        vm.deal(deployer, 100 ether);

        uint256 chainId = block.chainid;

        // 合约 A：用 CREATE 地址预测解决"根依赖地址、部署依赖根"的先有鸡先有蛋问题。
        address predictedA = vm.computeCreateAddress(deployer, vm.getNonce(deployer));
        bytes32[] memory leavesA = _dyn(_buildLeaves(predictedA, chainId));
        bytes32 rootA = TestMerkle.getRoot(leavesA);

        vm.prank(deployer);
        claimA = new MerkleClaim{value: _totalFunds()}(rootA);
        assertEq(address(claimA), predictedA, "A deployed at predicted address");

        // 合约 B：另一套分配（多一个叶子），独立的根，用于跨合约重放测试。
        address predictedB = vm.computeCreateAddress(deployer, vm.getNonce(deployer));
        bytes32[] memory leavesB = _dyn(_buildLeaves(predictedB, chainId));
        bytes32 rootB = TestMerkle.getRoot(_append(leavesB, keccak256("unrelated-ninth-leaf")));

        vm.prank(deployer);
        claimB = new MerkleClaim{value: _totalFunds()}(rootB);
        assertEq(address(claimB), predictedB, "B deployed at predicted address");

        vm.deal(executor, 1 ether); // 代领人自己持有 gas
    }

    function _append(bytes32[] memory arr, bytes32 extra)
        internal
        pure
        returns (bytes32[] memory out)
    {
        out = new bytes32[](arr.length + 1);
        for (uint256 i = 0; i < arr.length; i++) out[i] = arr[i];
        out[arr.length] = extra;
    }

    // ---------- 单笔 ----------

    function test_SingleClaim_SucceedsAndMarks() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        vm.expectEmit(true, true, false, true, address(claimA));
        emit MerkleClaim.Claimed(0, alice, 1 ether);

        vm.prank(executor);
        claimA.claim(0, alice, 1 ether, _proof(leaves, 0));

        assertTrue(claimA.isClaimed(0));
        assertEq(alice.balance, 1 ether);
    }

    function test_SingleClaim_RevertWhenDuplicateIndex() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        vm.prank(executor);
        claimA.claim(1, bob, 2 ether, _proof(leaves, 1));

        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, uint256(1)));
        vm.prank(executor);
        claimA.claim(1, bob, 2 ether, _proof(leaves, 1));
    }

    function test_SingleClaim_RevertWhenAmountTampered() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        vm.expectRevert(
            abi.encodeWithSelector(
                MerkleClaim.InvalidProof.selector, uint256(2), carol, 3 ether + 1
            )
        );
        vm.prank(executor);
        claimA.claim(2, carol, 3 ether + 1, _proof(leaves, 2));

        assertFalse(claimA.isClaimed(2), "failed claim must not mark bitmap");
        assertEq(carol.balance, 0);
    }

    function test_SingleClaim_RevertWhenAccountTampered() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, bob, 1 ether)
        );
        vm.prank(executor);
        // 拿 alice 的证明、索引领 bob 的地址
        claimA.claim(0, bob, 1 ether, _proof(leaves, 0));
    }

    // ---------- 跨合约 / 跨链重放 ----------

    function test_CrossContract_ReplayProofFromAtoB_Reverts() public {
        bytes32[] memory leavesA = _dyn(_buildLeaves(address(claimA), block.chainid));

        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, alice, 1 ether)
        );
        vm.prank(executor);
        claimB.claim(0, alice, 1 ether, _proof(leavesA, 0));

        assertFalse(claimB.isClaimed(0));
        assertEq(alice.balance, 0);
    }

    function test_CrossContract_ReplayProofFromBtoA_Reverts() public {
        bytes32[] memory leavesB = _dyn(_buildLeaves(address(claimB), block.chainid));

        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 4, dave, 4 ether)
        );
        vm.prank(executor);
        claimA.claim(4, dave, 4 ether, _proof(leavesB, 4));
    }

    function test_CrossChain_ProofForOtherChainId_Reverts() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        // 证明是在 chainid=31337 下构造的；切到主网 ID 后叶子不再匹配根。
        vm.chainId(1);
        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, alice, 1 ether)
        );
        vm.prank(executor);
        claimA.claim(0, alice, 1 ether, _proof(leaves, 0));
    }

    // ---------- 批量 ----------

    function test_Batch_HappyPath() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        uint256[] memory idx = new uint256[](2);
        address[] memory acc = new address[](2);
        uint256[] memory amt = new uint256[](2);
        bytes32[][] memory proofs = new bytes32[][](2);
        idx[0] = 1; idx[1] = 2;
        acc[0] = bob; acc[1] = carol;
        amt[0] = 2 ether; amt[1] = 3 ether;
        proofs[0] = _proof(leaves, 1);
        proofs[1] = _proof(leaves, 2);

        vm.expectEmit(true, true, false, true, address(claimA));
        emit MerkleClaim.Claimed(1, bob, 2 ether);
        vm.expectEmit(true, true, false, true, address(claimA));
        emit MerkleClaim.Claimed(2, carol, 3 ether);

        vm.prank(executor);
        claimA.claimBatch(idx, acc, amt, proofs);

        assertTrue(claimA.isClaimed(1));
        assertTrue(claimA.isClaimed(2));
        assertEq(bob.balance, 2 ether);
        assertEq(carol.balance, 3 ether);
    }

    function test_Batch_RevertWhenDuplicateIndexInsideBatch() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        uint256[] memory idx = new uint256[](2);
        address[] memory acc = new address[](2);
        uint256[] memory amt = new uint256[](2);
        bytes32[][] memory proofs = new bytes32[][](2);
        idx[0] = 0; idx[1] = 0;
        acc[0] = alice; acc[1] = alice;
        amt[0] = 1 ether; amt[1] = 1 ether;
        proofs[0] = _proof(leaves, 0);
        proofs[1] = _proof(leaves, 0);

        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, 0));
        vm.prank(executor);
        claimA.claimBatch(idx, acc, amt, proofs);

        // 原子性：第二项失败，第一项的位图标记与转账都必须回滚。
        assertFalse(claimA.isClaimed(0));
        assertEq(alice.balance, 0);
    }

    function test_Batch_RevertWhenOneProofInvalid_NoPartialState() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        uint256[] memory idx = new uint256[](2);
        address[] memory acc = new address[](2);
        uint256[] memory amt = new uint256[](2);
        bytes32[][] memory proofs = new bytes32[][](2);
        idx[0] = 0; idx[1] = 5;
        acc[0] = alice; acc[1] = erin;
        amt[0] = 1 ether; amt[1] = 5 ether + 1; // 篡改数量 → 叶子对不上根
        proofs[0] = _proof(leaves, 0);
        proofs[1] = _proof(leaves, 5);

        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 5, erin, 5 ether + 1)
        );
        vm.prank(executor);
        claimA.claimBatch(idx, acc, amt, proofs);

        assertFalse(claimA.isClaimed(0), "first item must roll back");
        assertFalse(claimA.isClaimed(5));
        assertEq(alice.balance, 0);
        assertEq(erin.balance, 0);
    }

    function test_Batch_RevertWhenIndexAlreadyClaimedBeforeBatch() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));

        vm.prank(executor);
        claimA.claim(0, alice, 1 ether, _proof(leaves, 0));

        uint256[] memory idx = new uint256[](2);
        address[] memory acc = new address[](2);
        uint256[] memory amt = new uint256[](2);
        bytes32[][] memory proofs = new bytes32[][](2);
        idx[0] = 0; idx[1] = 1;
        acc[0] = alice; acc[1] = bob;
        amt[0] = 1 ether; amt[1] = 2 ether;
        proofs[0] = _proof(leaves, 0);
        proofs[1] = _proof(leaves, 1);

        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, 0));
        vm.prank(executor);
        claimA.claimBatch(idx, acc, amt, proofs);

        // bob 虽然在批次里合法，但整批回滚，不能留下已领状态。
        assertFalse(claimA.isClaimed(1));
        assertEq(bob.balance, 0);
        assertEq(alice.balance, 1 ether); // 之前的单笔领取不受影响
    }

    function test_Batch_RevertWhenOneTransferFails_NoEthMoved() public {
        bytes32[] memory leaves = _dyn(_buildLeaves(address(claimA), block.chainid));
        uint256 contractBalanceBefore = address(claimA).balance;

        uint256[] memory idx = new uint256[](2);
        address[] memory acc = new address[](2);
        uint256[] memory amt = new uint256[](2);
        bytes32[][] memory proofs = new bytes32[][](2);
        idx[0] = 0; idx[1] = 3; // 3 = rejecter，转账必然失败
        acc[0] = alice; acc[1] = address(rejecter);
        amt[0] = 1 ether; amt[1] = 0.5 ether;
        proofs[0] = _proof(leaves, 0);
        proofs[1] = _proof(leaves, 3);

        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.TransferFailed.selector, address(rejecter), 0.5 ether)
        );
        vm.prank(executor);
        claimA.claimBatch(idx, acc, amt, proofs);

        assertFalse(claimA.isClaimed(0));
        assertFalse(claimA.isClaimed(3));
        assertEq(alice.balance, 0);
        assertEq(address(claimA).balance, contractBalanceBefore, "contract funds untouched");
    }

    function test_Batch_RevertOnLengthMismatch() public {
        uint256[] memory idx = new uint256[](1);
        address[] memory acc = new address[](2);
        uint256[] memory amt = new uint256[](1);
        bytes32[][] memory proofs = new bytes32[][](1);

        vm.expectRevert(
            abi.encodeWithSelector(MerkleClaim.LengthMismatch.selector, 1, 2, 1, 1)
        );
        vm.prank(executor);
        claimA.claimBatch(idx, acc, amt, proofs);
    }

    function test_Batch_RevertOnEmptyBatch() public {
        vm.expectRevert(MerkleClaim.EmptyBatch.selector);
        vm.prank(executor);
        claimA.claimBatch(
            new uint256[](0), new address[](0), new uint256[](0), new bytes32[][](0)
        );
    }
}

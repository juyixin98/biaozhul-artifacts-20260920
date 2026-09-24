// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {MerkleClaim} from "../src/MerkleClaim.sol";
import {TreeBuilder} from "./helpers/TreeBuilder.sol";
import {ClaimFactory} from "./helpers/ClaimFactory.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {ReentrantClaimant} from "./mocks/ReentrantClaimant.sol";

contract MerkleClaimTest is Test {
    MerkleClaim internal claimC; // 原生币模式
    MerkleClaim internal claimB; // 第二实例（跨合约重放）
    MerkleClaim internal claimToken; // ERC20 模式
    MockERC20 internal token;
    ClaimFactory internal factory;

    address internal owner = address(0xA11CE);

    address[8] internal accts = [
        address(0x1000000000000000000000000000000000000001),
        address(0x1000000000000000000000000000000000000002),
        address(0x1000000000000000000000000000000000000003),
        address(0x1000000000000000000000000000000000000004),
        address(0x1000000000000000000000000000000000000005),
        address(0x1000000000000000000000000000000000000006),
        address(0x1000000000000000000000000000000000000007),
        address(0x1000000000000000000000000000000000000008)
    ];
    uint256[8] internal amts =
        [1 ether, 2 ether, 3 ether, 4 ether, 5 ether, 6 ether, 7 ether, 8 ether];

    function setUp() public {
        factory = new ClaimFactory();

        // CREATE 地址只依赖 (factory, nonce)，与构造参数 root 无关：
        // 预测下一个部署地址 → 为该地址生成绑定叶子的根 → 部署，地址必然吻合。
        claimC = _newClaim(address(0));
        claimB = _newClaim(address(0)); // 第二实例（跨合约重放测试）
        token = new MockERC20();
        claimToken = _newClaim(address(token));

        vm.deal(address(claimC), 100 ether);
        vm.deal(address(claimB), 100 ether);
        token.mint(address(claimToken), 1000 ether);
    }

    /// @dev 预测工厂下一个 CREATE 地址，用绑定该地址的 8 项分配根部署一个 MerkleClaim
    function _newClaim(address tokenAddr) internal returns (MerkleClaim c) {
        address pred = _predictNext();
        bytes32 r = _rootFor(pred);
        c = MerkleClaim(payable(factory.deploy(r, tokenAddr, owner)));
        require(address(c) == pred, "CREATE address mismatch");
    }

    /// @dev 工厂下一个 CREATE 地址（与构造参数无关）
    function _predictNext() internal view returns (address) {
        return vm.computeCreateAddress(address(factory), vm.getNonce(address(factory)));
    }

    // ---- Merkle 构造辅助（每次新建 builder，因 TreeBuilder 只能 build 一次）----

    function _leaf(address forAddr, uint256 idx, address acct, uint256 amt)
        internal
        view
        returns (bytes32)
    {
        return keccak256(abi.encodePacked(block.chainid, forAddr, idx, acct, amt));
    }

    function _rootFor(address forAddr) internal returns (bytes32 r) {
        TreeBuilder t = new TreeBuilder();
        for (uint256 i = 0; i < accts.length; i++) t.addLeaf(i, _leaf(forAddr, i, accts[i], amts[i]));
        r = t.root();
    }

    function _proofFor(address forAddr, uint256 idx) internal returns (bytes32[] memory p) {
        TreeBuilder t = new TreeBuilder();
        for (uint256 i = 0; i < accts.length; i++) t.addLeaf(i, _leaf(forAddr, i, accts[i], amts[i]));
        t.root();
        p = t.proofFor(idx);
    }

    function _batch(address forAddr, uint256[3] memory idxs)
        internal
        returns (
            uint256[] memory indices,
            address[] memory accounts,
            uint256[] memory amounts,
            bytes32[][] memory proofs
        )
    {
        indices = new uint256[](3);
        accounts = new address[](3);
        amounts = new uint256[](3);
        proofs = new bytes32[][](3);
        for (uint256 i = 0; i < 3; i++) {
            uint256 idx = idxs[i];
            indices[i] = idx;
            accounts[i] = accts[idx];
            amounts[i] = amts[idx];
            proofs[i] = _proofFor(forAddr, idx);
        }
    }

    // ================================================================
    // 叶子绑定
    // ================================================================

    function test_leafHash_BindsAllFields() public view {
        bytes32 h = claimC.leafHash(0, accts[0], amts[0]);
        assertEq(h, keccak256(abi.encodePacked(block.chainid, address(claimC), uint256(0), accts[0], amts[0])));
        assertTrue(claimC.leafHash(0, accts[1], amts[0]) != h);
        assertTrue(claimC.leafHash(0, accts[0], amts[0] + 1) != h);
        assertTrue(claimC.leafHash(1, accts[0], amts[0]) != h);
    }

    // ================================================================
    // 单项领取
    // ================================================================

    function test_claim_Single() public {
        uint256 beforeBal = accts[0].balance;
        claimC.claim(0, accts[0], amts[0], _proofFor(address(claimC), 0));
        assertEq(accts[0].balance, beforeBal + 1 ether);
        assertTrue(claimC.isClaimed(0));
        assertEq(claimC.claimedWord(0), uint256(1));
    }

    function test_claim_DuplicateIndex_Reverts() public {
        bytes32[] memory p = _proofFor(address(claimC), 0);
        claimC.claim(0, accts[0], amts[0], p);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, 0));
        claimC.claim(0, accts[0], amts[0], p);
    }

    function test_claim_WrongAmount_Reverts() public {
        bytes32[] memory p = _proofFor(address(claimC), 0);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, accts[0], 2 ether));
        claimC.claim(0, accts[0], 2 ether, p);
        assertFalse(claimC.isClaimed(0));
    }

    function test_claim_WrongAccount_Reverts() public {
        bytes32[] memory p = _proofFor(address(claimC), 0);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, accts[1], 1 ether));
        claimC.claim(0, accts[1], 1 ether, p);
    }

    function test_claim_ForgedProof_Reverts() public {
        bytes32[] memory p = _proofFor(address(claimC), 1);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, accts[0], 1 ether));
        claimC.claim(0, accts[0], 1 ether, p);
    }

    // ================================================================
    // 跨合约重放
    // ================================================================

    function test_claim_CrossContractReplay_Reverts() public {
        bytes32[] memory pA = _proofFor(address(claimC), 0);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 0, accts[0], 1 ether));
        claimB.claim(0, accts[0], 1 ether, pA);
        claimC.claim(0, accts[0], amts[0], _proofFor(address(claimC), 0));
        assertTrue(claimC.isClaimed(0));
        assertFalse(claimB.isClaimed(0));
    }

    // ================================================================
    // 跨链重放
    // ================================================================

    function test_claim_CrossChainReplay_Reverts() public {
        claimC.claim(0, accts[0], amts[0], _proofFor(address(claimC), 0));
        bytes32[] memory p1 = _proofFor(address(claimC), 1);
        vm.chainId(4242);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.WrongChain.selector, uint256(31337), uint256(4242)));
        claimC.claim(1, accts[1], amts[1], p1);
        assertFalse(claimC.isClaimed(1));
    }

    // ================================================================
    // 批量领取
    // ================================================================

    function test_claimBatch_Success() public {
        uint256[3] memory idxs = [uint256(0), 1, 2];
        (uint256[] memory i, address[] memory a, uint256[] memory m, bytes32[][] memory p) =
            _batch(address(claimC), idxs);
        claimC.claimBatch(i, a, m, p);
        for (uint256 k = 0; k < 3; k++) {
            assertTrue(claimC.isClaimed(i[k]));
            assertEq(accts[k].balance, amts[k]);
        }
    }

    function test_claimBatch_InvalidItem_RevertsAll() public {
        uint256[3] memory idxs = [uint256(0), 1, 2];
        (uint256[] memory i, address[] memory a, uint256[] memory m, bytes32[][] memory p) =
            _batch(address(claimC), idxs);
        m[1] = 99 ether; // 破坏中间项
        uint256 beforeBal = address(claimC).balance;
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 1, accts[1], 99 ether));
        claimC.claimBatch(i, a, m, p);
        for (uint256 k = 0; k < 3; k++) {
            assertFalse(claimC.isClaimed(k));
            assertEq(accts[k].balance, 0);
        }
        assertEq(address(claimC).balance, beforeBal);
    }

    function test_claimBatch_DuplicateIndexInsideBatch_RevertsAll() public {
        uint256[3] memory idxs = [uint256(0), 1, 2];
        (uint256[] memory i, address[] memory a, uint256[] memory m, bytes32[][] memory p) =
            _batch(address(claimC), idxs);
        i[1] = 0;
        a[1] = accts[0];
        m[1] = amts[0];
        p[1] = _proofFor(address(claimC), 0);
        uint256 beforeBal = address(claimC).balance;
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, 0));
        claimC.claimBatch(i, a, m, p);
        assertFalse(claimC.isClaimed(0));
        assertEq(address(claimC).balance, beforeBal);
    }

    function test_claimBatch_DuplicateAcrossTx_Reverts() public {
        uint256[3] memory idxs1 = [uint256(0), 1, 2];
        (uint256[] memory i1, address[] memory a1, uint256[] memory m1, bytes32[][] memory p1) =
            _batch(address(claimC), idxs1);
        claimC.claimBatch(i1, a1, m1, p1);

        uint256[3] memory idxs2 = [uint256(3), 1, 4]; // 索引 1 已领
        (uint256[] memory i2, address[] memory a2, uint256[] memory m2, bytes32[][] memory p2) =
            _batch(address(claimC), idxs2);
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, 1));
        claimC.claimBatch(i2, a2, m2, p2);
        // 第二笔整体回滚：3 和 4 未被领
        assertFalse(claimC.isClaimed(3));
        assertFalse(claimC.isClaimed(4));
        assertEq(accts[3].balance, 0);
        assertEq(accts[4].balance, 0);
    }

    function test_claimBatch_EmptyAndLengthMismatch() public {
        uint256[] memory eI = new uint256[](0);
        address[] memory eA = new address[](0);
        uint256[] memory eM = new uint256[](0);
        bytes32[][] memory eP = new bytes32[][](0);
        vm.expectRevert(MerkleClaim.EmptyBatch.selector);
        claimC.claimBatch(eI, eA, eM, eP);

        uint256[] memory oneI = new uint256[](1);
        oneI[0] = 0;
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.LengthMismatch.selector, 1, 0, 0, 0));
        claimC.claimBatch(oneI, eA, eM, eP);
    }

    // ================================================================
    // 大索引位图
    // ================================================================

    function test_claim_LargeIndex_BitmapWord() public {
        uint256 big = 300; // word 1, bit 44
        address pred = _predictNext();
        TreeBuilder t = new TreeBuilder();
        t.addLeaf(big, _leaf(pred, big, accts[3], 0.5 ether));
        MerkleClaim c = MerkleClaim(payable(factory.deploy(t.root(), address(0), owner)));
        assertEq(address(c), pred);
        vm.deal(address(c), 10 ether);
        bytes32[] memory p = t.proofFor(big);

        c.claim(big, accts[3], 0.5 ether, p);
        assertTrue(c.isClaimed(big));
        assertFalse(c.isClaimed(0));
        assertEq(c.claimedWord(1), uint256(1) << 44);

        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.AlreadyClaimed.selector, big));
        c.claim(big, accts[3], 0.5 ether, p);
    }

    // ================================================================
    // 重入
    // ================================================================

    function test_claim_ReentrancySameIndex_Reverts() public {
        // 预测 claim 地址 → 先用该地址部署 attacker → 再用绑定 attacker 叶子的根部署 claim
        address predAddr = _predictNext();
        ReentrantClaimant atk = new ReentrantClaimant(predAddr);
        TreeBuilder t = new TreeBuilder();
        t.addLeaf(0, _leaf(predAddr, 0, address(atk), 1 ether));
        MerkleClaim c = MerkleClaim(payable(factory.deploy(t.root(), address(0), owner)));
        assertEq(address(c), predAddr);
        vm.deal(address(c), 10 ether);
        bytes32[] memory p = t.proofFor(0);
        atk.configureSame(0, 1 ether, p);

        uint256 beforeBal = address(c).balance;
        vm.expectRevert(); // 重入的 claim 因 AlreadyClaimed 回滚 → 外层收款失败
        atk.doClaim(0, 1 ether, p);
        assertEq(address(c).balance, beforeBal);
        assertFalse(c.isClaimed(0));
        assertEq(address(atk).balance, 0);
    }

    function test_claimBatch_ReentrancyOtherIndex_RevertsAll() public {
        address predAddr = _predictNext();
        ReentrantClaimant atk = new ReentrantClaimant(predAddr);

        TreeBuilder t = new TreeBuilder();
        for (uint256 k = 0; k < 3; k++) {
            t.addLeaf(k, _leaf(predAddr, k, address(atk), 1 ether));
        }
        MerkleClaim c = MerkleClaim(payable(factory.deploy(t.root(), address(0), owner)));
        assertEq(address(c), predAddr);
        vm.deal(address(c), 10 ether);
        bytes32[][] memory ps = new bytes32[][](3);
        for (uint256 k = 0; k < 3; k++) ps[k] = t.proofFor(k);

        atk.configureOther(2, 1 ether, ps[2]);

        uint256[] memory ii = new uint256[](3);
        address[] memory aa = new address[](3);
        uint256[] memory mm = new uint256[](3);
        for (uint256 k = 0; k < 3; k++) {
            ii[k] = k;
            aa[k] = address(atk);
            mm[k] = 1 ether;
        }
        // 合约在付款前已标记全部 3 个索引（checks-effects-interactions）：
        // 攻击者在第 0 项收款时重入 claim(2) → 索引 2 已标记 → 重入回滚 →
        // 外层 call 返回 false → NativeTransferFailed，整笔批次完全回滚。
        vm.expectRevert(abi.encodeWithSelector(
            MerkleClaim.NativeTransferFailed.selector, address(atk), uint256(1 ether)));
        c.claimBatch(ii, aa, mm, ps);
        for (uint256 k = 0; k < 3; k++) assertFalse(c.isClaimed(k));
        assertEq(address(c).balance, 10 ether);
        assertEq(address(atk).balance, 0);
    }

    // ================================================================
    // ERC20 模式
    // ================================================================

    function test_claim_ERC20_Single() public {
        claimToken.claim(0, accts[0], amts[0], _proofFor(address(claimToken), 0));
        assertEq(token.balanceOf(accts[0]), 1 ether);
        assertTrue(claimToken.isClaimed(0));
    }

    function test_claim_ERC20_InvalidItem_RevertsAll() public {
        uint256[3] memory idxs = [uint256(0), 1, 2];
        (uint256[] memory i, address[] memory a, uint256[] memory m, bytes32[][] memory p) =
            _batch(address(claimToken), idxs);
        m[2] = 999 ether;
        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.InvalidProof.selector, 2, accts[2], 999 ether));
        claimToken.claimBatch(i, a, m, p);
        for (uint256 k = 0; k < 3; k++) {
            assertFalse(claimToken.isClaimed(k));
            assertEq(token.balanceOf(accts[k]), 0);
        }
    }

    function test_claim_ERC20_PaymentFailureRollsBack() public {
        // 合约只持有 10 ether 代币，却要付 7 + 8 = 15；第二项付款失败，整体回滚
        MockERC20 t2 = new MockERC20();
        address pred = _predictNext();
        MerkleClaim c = MerkleClaim(payable(factory.deploy(_rootFor(pred), address(t2), owner)));
        assertEq(address(c), pred);
        t2.mint(address(c), 10 ether);

        uint256[] memory ii = new uint256[](2);
        address[] memory aa = new address[](2);
        uint256[] memory mm = new uint256[](2);
        bytes32[][] memory pp = new bytes32[][](2);
        ii[0] = 6; ii[1] = 7;
        aa[0] = accts[6]; aa[1] = accts[7];
        mm[0] = 7 ether; mm[1] = 8 ether;
        pp[0] = _proofFor(address(c), 6);
        pp[1] = _proofFor(address(c), 7);

        vm.expectRevert(abi.encodeWithSelector(MerkleClaim.TokenTransferFailed.selector, accts[7], 8 ether));
        c.claimBatch(ii, aa, mm, pp);
        assertFalse(c.isClaimed(6));
        assertFalse(c.isClaimed(7));
        assertEq(t2.balanceOf(accts[6]), 0);
        assertEq(t2.balanceOf(address(c)), 10 ether);
    }

    function test_withdraw_OnlyOwner() public {
        vm.prank(accts[0]);
        vm.expectRevert(MerkleClaim.OnlyOwner.selector);
        claimC.withdraw(1 ether, false);

        uint256 beforeBal = owner.balance;
        vm.prank(owner);
        claimC.withdraw(1 ether, false);
        assertEq(owner.balance, beforeBal + 1 ether);
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {MockERC20} from "../src/MockERC20.sol";
import {ShareVault} from "../src/ShareVault.sol";

contract ShareVaultTest is Test {
    MockERC20 token;
    ShareVault vault;

    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    uint256 constant MIN_LIQUIDITY = 1000;

    function setUp() public {
        token = new MockERC20("Mock Token", "MOCK", 18);
        vault = new ShareVault(address(token), "Mock Share Vault", "sMOCK");
    }

    // 辅助：给 who 发钱并授权金库。
    function _fund(address who, uint256 amount) internal {
        deal(address(token), who, amount);
        vm.prank(who);
        token.approve(address(vault), type(uint256).max);
    }

    function _deposit(address who, uint256 assets, uint256 minShares) internal returns (uint256 shares) {
        vm.prank(who);
        shares = vault.deposit(assets, who, minShares);
    }

    // -----------------------------------------------------------------------
    // 空库初始化
    // -----------------------------------------------------------------------

    /// @dev 首次存款 1:1 铸份额，并锁死 MIN_LIQUIDITY 到 DEAD 地址。
    function test_firstDeposit_locksDeadShares() public {
        _fund(alice, 5_000 ether);
        uint256 shares = _deposit(alice, 5_000 ether, 0);
        assertEq(shares, 5_000 ether - MIN_LIQUIDITY, "first shares = assets - dead");
        assertEq(vault.totalSupply(), 5_000 ether, "supply includes dead shares");
        assertEq(vault.balanceOf(address(1)), MIN_LIQUIDITY, "dead shares locked");
        assertEq(vault.totalAssets(), 5_000 ether);
    }

    /// @dev 首次存款必须大于 MIN_LIQUIDITY。
    function testFuzz_firstDeposit_revertsWhenTooSmall(uint96 amount) public {
        vm.assume(amount >= 1 && amount <= MIN_LIQUIDITY);
        _fund(alice, uint256(amount) + 1);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(ShareVault.FirstDepositTooSmall.selector, MIN_LIQUIDITY + 1, amount)
        );
        vault.deposit(amount, alice, 0);
    }

    // -----------------------------------------------------------------------
    // 极小存款：向下取整产生 0 份额时必须回滚，资产不得被吞
    // -----------------------------------------------------------------------

    function test_tinyDeposit_zeroShares_reverts_andKeepsAssets() public {
        // 首存 1,000,000（含 1000 死份额），再捐赠 999 倍把汇率抬到 1000:1。
        _fund(alice, 1_000_000 + 999_000_000);
        _deposit(alice, 1_000_000, 0);
        vm.prank(alice);
        token.transfer(address(vault), 999_000_000);

        assertEq(vault.totalAssets() / vault.totalSupply(), 1000, "rate inflated to 1000:1");

        // 999 wei 存款按 floor(999*supply/totalAssets) = 0，必须回滚 ZeroShares。
        _fund(bob, 999);
        uint256 balBefore = token.balanceOf(bob);
        uint256 assetsBefore = vault.totalAssets();
        vm.prank(bob);
        vm.expectRevert(ShareVault.ZeroShares.selector);
        vault.deposit(999, bob, 0);
        assertEq(token.balanceOf(bob), balBefore, "tiny depositor keeps assets");
        assertEq(vault.totalAssets(), assetsBefore, "vault assets unchanged");
        assertEq(vault.balanceOf(bob), 0, "no shares minted");

        // 恰好 1000 wei 可铸出 1 份额（边界）。
        _fund(bob, 1000);
        vm.prank(bob);
        uint256 sh = vault.deposit(1000, bob, 0);
        assertEq(sh, 1, "boundary deposit mints exactly 1 share");
    }

    // -----------------------------------------------------------------------
    // 舍入方向：存款份额 <= 精确比例；赎回资产 <= 精确比例
    // -----------------------------------------------------------------------

    /// @dev 任意后续存款：shares = floor(assets*supply/totalAssets)，恒有
    ///      shares*totalAssets <= assetsIn*supply（舍入对存款者不利、对旧份额有利）。
    function testFuzz_depositRoundsDown(uint256 first, uint256 second) public {
        first = bound(first, MIN_LIQUIDITY + 1, 1e30);
        second = bound(second, 1, 1e30);
        _fund(alice, first + second);
        _deposit(alice, first, 0);

        uint256 supplyBefore = vault.totalSupply();
        uint256 assetsBefore = vault.totalAssets();
        uint256 shares = _deposit(alice, second, 0);

        assertGe(shares, 1, "non-zero deposit mints shares");
        assertLe(
            shares * assetsBefore,
            second * supplyBefore,
            "deposit rounds down against depositor"
        );
        assertEq(vault.previewDeposit(second), shares, "previewDeposit matches actual");
    }

    /// @dev 任意赎回：assetsOut = floor(shares*totalAssets/supply)，
    ///      assetsOut*supply <= shares*totalAssets（金库不会因赎回被多拿）。
    function testFuzz_redeemRoundsDown(uint256 depositAmt, uint256 redeemShares) public {
        depositAmt = bound(depositAmt, MIN_LIQUIDITY + 1, 1e30);
        _fund(alice, depositAmt);
        uint256 minted = _deposit(alice, depositAmt, 0);
        redeemShares = bound(redeemShares, 1, minted);

        uint256 assetsBefore = vault.totalAssets();
        uint256 supplyBefore = vault.totalSupply();
        vm.prank(alice);
        uint256 out = vault.redeem(redeemShares, alice, alice, 0);

        assertLe(
            out * supplyBefore,
            redeemShares * assetsBefore,
            "redeem rounds down against redeemer"
        );
        assertEq(vault.previewRedeem(redeemShares), out, "previewRedeem matches actual");
    }

    // -----------------------------------------------------------------------
    // 单人存-赎往返：最终拿回的资产 <= 存入；差额仅为死份额 + 至多 2 wei 舍入
    // -----------------------------------------------------------------------

    function testFuzz_singleUserRoundTripDust(uint256 amount, uint256 split) public {
        amount = bound(amount, MIN_LIQUIDITY + 1, 1e30);
        _fund(alice, amount);
        uint256 shares = _deposit(alice, amount, 0);
        split = bound(split, 1, shares);

        vm.prank(alice);
        uint256 out1 = vault.redeem(split, alice, alice, 0);
        uint256 remaining = shares - split;
        uint256 out2 = 0;
        if (remaining > 0) {
            vm.prank(alice);
            out2 = vault.redeem(remaining, alice, alice, 0);
        }
        assertLe(out1 + out2, amount, "cannot redeem more than deposited");
        // 首存 1:1：死份额恰持 1000 wei 背书；两次向下取整各至多留 1 wei。
        assertLe(
            amount - (out1 + out2),
            MIN_LIQUIDITY + 2,
            "loss bounded by dead shares + rounding dust"
        );
    }

    // -----------------------------------------------------------------------
    // 全额赎回：销毁全部用户份额后，死份额仍锁库且保持背书
    // -----------------------------------------------------------------------

    function testFuzz_fullRedeem_leavesDeadShares(uint96 amount) public {
        uint256 amt = bound(uint256(amount), MIN_LIQUIDITY + 1, 1e24);
        _fund(alice, amt);
        uint256 shares = _deposit(alice, amt, 0);

        vm.prank(alice);
        vault.redeem(shares, alice, alice, 0);

        assertEq(vault.balanceOf(alice), 0, "user has no shares left");
        assertEq(vault.totalSupply(), MIN_LIQUIDITY, "dead shares remain after full redeem");
        assertEq(vault.balanceOf(address(1)), MIN_LIQUIDITY, "DEAD still holds locked shares");
        assertGe(vault.totalAssets(), MIN_LIQUIDITY, "dead shares stay backed");
    }

    // -----------------------------------------------------------------------
    // 首次捐赠攻击（空库通胀攻击）：攻击者最小首存 + 大额捐赠不能获利，
    // 受害者本金（差 1 wei 以内）受到保护。
    // -----------------------------------------------------------------------

    function test_firstDonationInflationAttack_isUnprofitable() public {
        address attacker = makeAddr("attacker");
        address victim = makeAddr("victim");

        // 攻击者尝试最小首存 1001（得到 1 份额），随后捐赠 1,000,000 枚制造虚高汇率。
        _fund(attacker, 1001 + 1_000_000 ether);
        vm.prank(attacker);
        uint256 atkShares = vault.deposit(1001, attacker, 0);
        assertEq(atkShares, 1, "attacker gets only 1 share from minimum first deposit");
        vm.prank(attacker);
        token.transfer(address(vault), 1_000_000 ether);

        // 受害者以正常金额 1000 ether 加入。
        _fund(victim, 1_000 ether);
        vm.prank(victim);
        uint256 victimShares = vault.deposit(1_000 ether, victim, 0);

        vm.prank(victim);
        uint256 victimBack = vault.redeem(victimShares, victim, victim, 0);
        vm.prank(attacker);
        uint256 atkBack = vault.redeem(atkShares, attacker, attacker, 0);

        // 受害者损失被钉死在“死份额 / 首存总份额”这一固定比例附近（~0.1%），
        // 与攻击者的捐赠规模完全无关——捐得再多也不能让受害者多亏 1 wei。
        uint256 victimLoss = 1_000 ether - victimBack;
        // 首存后总份额为 1001：受害者相对损失上界 = 1000/1001 + 每次取整 1 wei。
        uint256 maxLoss = (uint256(1_000 ether) * 1000) / 1001 + 1;
        assertLe(victimLoss, maxLoss, "victim loss bounded by dead-share ratio, independent of donation");
        // 攻击者总投入 = 1001 wei 首存 + 1,000,000 枚捐赠；仅持 1 份额，面对
        // 1000 死份额只能按比例瓜分库产，整体净亏近全部捐赠。
        uint256 atkInvested = 1001 + 1_000_000 ether;
        assertLt(atkBack, atkInvested, "attacker never recovers investment");
        assertLt(atkBack * 1000, 1_000_000 ether, "attacker recovers < 0.1% of donation");
        emit log_named_decimal_uint("attacker donated", 1_000_000 ether, 18);
        emit log_named_decimal_uint("attacker recovered", atkBack, 18);
        emit log_named_decimal_uint("victim recovered", victimBack, 18);
        emit log_named_decimal_uint("victim loss", victimLoss, 18);
    }

    /// @dev 对完全空库的直接捐赠不会铸造任何份额；首存仍按 1:1 初始化。
    function test_donationToEmptyVault_mintsNoShares() public {
        _fund(alice, 100 ether);
        vm.prank(alice);
        token.transfer(address(vault), 100 ether);
        assertEq(vault.totalSupply(), 0, "donation to empty vault mints no shares");

        _fund(bob, 1_000 ether);
        vm.prank(bob);
        uint256 shares = vault.deposit(1_000 ether, bob, 0);
        assertEq(shares, 1_000 ether - MIN_LIQUIDITY, "first deposit still 1:1");
        assertEq(vault.totalAssets(), 1_100 ether, "donation stays inside vault");
    }

    // -----------------------------------------------------------------------
    // 最大滑点：实际结果低于用户下界时整笔回滚
    // -----------------------------------------------------------------------

    function test_deposit_slippageReverts() public {
        _fund(alice, 2_000 ether);
        _deposit(alice, 1_000 ether, 0);
        deal(address(token), address(vault), vault.totalAssets() + 1_000 ether);

        _fund(bob, 1_000 ether);
        uint256 expected = vault.previewDeposit(1_000 ether);
        vm.prank(bob);
        vm.expectRevert(
            abi.encodeWithSelector(ShareVault.SlippageExceeded.selector, expected + 1, expected)
        );
        vault.deposit(1_000 ether, bob, expected + 1);
    }

    function test_redeem_slippageReverts() public {
        _fund(alice, 2_000 ether);
        uint256 shares = _deposit(alice, 2_000 ether, 0);
        uint256 expected = vault.previewRedeem(shares);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(ShareVault.SlippageExceeded.selector, expected + 1, expected)
        );
        vault.redeem(shares, alice, alice, expected + 1);
    }

    // -----------------------------------------------------------------------
    // 多人场景 + 中途捐赠：两人分别赎回后，库内仍保留死份额背书（不资不抵债）
    // -----------------------------------------------------------------------

    function testFuzz_twoDepositors_solvencyAfterRedemptions(
        uint256 amtA,
        uint256 amtB,
        uint256 donation
    ) public {
        amtA = bound(amtA, 1_000 ether, 100_000 ether);
        amtB = bound(amtB, 1 ether, 100_000 ether);
        donation = bound(donation, 0, 50_000 ether);

        _fund(alice, amtA);
        _fund(bob, amtB + donation);
        uint256 shA = _deposit(alice, amtA, 0);
        if (donation > 0) {
            vm.prank(bob);
            token.transfer(address(vault), donation);
        }
        uint256 shB = _deposit(bob, amtB, 0);

        vm.prank(alice);
        uint256 backA = vault.redeem(shA, alice, alice, 0);
        vm.prank(bob);
        uint256 backB = vault.redeem(shB, bob, bob, 0);

        assertGe(vault.totalAssets(), MIN_LIQUIDITY, "vault never becomes insolvent");
        assertEq(vault.totalSupply(), MIN_LIQUIDITY, "only dead shares remain");
        // 两人合计取走不超过“库产 - 死份额背书”。
        assertLe(backA + backB, amtA + amtB + donation - MIN_LIQUIDITY + 2, "collective solvency");
    }
}

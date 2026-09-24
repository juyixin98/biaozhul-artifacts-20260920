// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ShareVault} from "../src/ShareVault.sol";
import {MockERC20} from "../src/MockERC20.sol";
import {Math512} from "../src/Math512.sol";
import {ReentrantERC20} from "./mocks/ReentrantERC20.sol";
import {FeeOnTransferERC20} from "./mocks/FeeOnTransferERC20.sol";

contract ShareVaultTest is Test {
    MockERC20 internal token;
    ShareVault internal vault;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal attacker = makeAddr("attacker");
    address internal victim = makeAddr("victim");

    uint256 internal constant V = 1000; // VIRTUAL_SHARE_OFFSET
    uint256 internal constant CAP = 1e30; // fuzz amount cap, keeps ghosts < 2^256

    function setUp() public {
        token = new MockERC20();
        vault = new ShareVault(address(token), "Vault Share", "vMOCK");
    }

    function _fund(address who, uint256 amount) internal {
        token.mint(who, amount);
        vm.prank(who);
        token.approve(address(vault), type(uint256).max);
    }

    function _deposit(address who, uint256 assets) internal returns (uint256 shares) {
        vm.prank(who);
        shares = vault.deposit(assets, who, 0);
    }

    function _redeem(address who, uint256 shares) internal returns (uint256 assets) {
        vm.prank(who);
        assets = vault.redeem(shares, who, 0);
    }

    function _donate(address who, uint256 amount) internal {
        token.mint(who, amount);
        vm.prank(who);
        token.transfer(address(vault), amount);
    }

    /*//////////////////////////////////////////////////////////////
                        TINY DEPOSITS / EMPTY VAULT
    //////////////////////////////////////////////////////////////*/

    /// Empty vault: 1 wei of asset must work and round-trip exactly.
    function test_tinyDeposit_oneWei_roundTrips() public {
        _fund(alice, 1);
        uint256 shares = _deposit(alice, 1);
        assertEq(shares, V, "1 wei on empty vault mints VIRTUAL_SHARE_OFFSET shares");
        assertEq(vault.totalAssets(), 1);

        uint256 out = _redeem(alice, shares);
        assertEq(out, 1, "redeem returns the exact wei back");
        assertEq(vault.totalSupply(), 0);
        assertEq(vault.totalAssets(), 0);
    }

    function test_tinyDeposits_exact() public {
        uint256[6] memory amounts = [uint256(1), 2, 3, 5, 10, 100];
        for (uint256 i = 0; i < amounts.length; i++) {
            uint256 a = amounts[i];
            _fund(alice, a);
            uint256 shares = _deposit(alice, a);
            // No donations yet: rate stays exactly 1 asset = 1000 shares.
            assertEq(shares, a * V);
            uint256 out = _redeem(alice, shares);
            assertEq(out, a);
        }
    }

    /// Zero-asset deposits and zero-share redeems are rejected.
    function test_zeroAmounts_revert() public {
        vm.expectRevert(ShareVault.ZeroAssets.selector);
        vault.deposit(0, alice, 0);
        vm.expectRevert(ShareVault.ZeroShares.selector);
        vault.redeem(0, alice, 0);
    }

    /*//////////////////////////////////////////////////////////////
                    ROUND TRIP / ROUNDING DIRECTION (FUZZ)
    //////////////////////////////////////////////////////////////*/

    /// Deposit then redeem everything: the user can never get back MORE than
    /// they put in (rounding always favors the pool, never the user).
    function testFuzz_depositRedeem_roundTrip_neverProfits(uint256 amount) public {
        amount = bound(amount, 1, CAP);
        _fund(alice, amount);
        uint256 shares = _deposit(alice, amount);
        assertGt(shares, 0);
        uint256 out = _redeem(alice, shares);
        assertLe(out, amount, "round trip must not create assets");
        // On a clean vault (no donations) the first depositor round-trips exactly.
        assertEq(out, amount);
    }

    /// Two depositors, second one redeems fully: gets back at most their input.
    function testFuzz_secondDepositor_roundingFavorsPool(uint256 a, uint256 b) public {
        a = bound(a, 1, CAP);
        b = bound(b, 1, CAP);
        _fund(alice, a);
        _fund(bob, b);
        _deposit(alice, a);
        uint256 sharesB = _deposit(bob, b);
        uint256 outB = _redeem(bob, sharesB);
        assertLe(outB, b, "redeem floors: bob cannot take out more than he put in");
        // Alice's remaining shares are still fully backed.
        assertGe(vault.totalAssets(), vault.convertToAssets(vault.balanceOf(alice)));
    }

    /// Rounding direction, definitionally: minted shares never exceed the
    /// exact ratio, paid assets never exceed the exact ratio.
    function testFuzz_roundingDirection(uint256 a, uint256 donation, uint256 b) public {
        a = bound(a, 1, CAP);
        donation = bound(donation, 0, CAP);
        b = bound(b, 1, CAP);
        _fund(alice, a);
        _deposit(alice, a);
        if (donation > 0) _donate(attacker, donation);

        uint256 supply = vault.totalSupply();
        uint256 assets = vault.totalAssets();

        uint256 quotedShares = vault.convertToShares(b);
        // shares <= b * (S + V) / (A + 1)  (floor)
        assertLe(quotedShares * (assets + 1), b * (supply + V));

        _fund(bob, b);
        vm.prank(bob);
        uint256 got = vault.deposit(b, bob, 0);
        assertEq(got, quotedShares, "deposit matches preview");

        uint256 quotedAssets = vault.convertToAssets(got);
        assertLe(quotedAssets * (vault.totalSupply() + V), got * (vault.totalAssets() + 1));
    }

    /*//////////////////////////////////////////////////////////////
                     FIRST-DEPOSITOR DONATION ATTACK
    //////////////////////////////////////////////////////////////*/

    /// Canonical inflation attack: deposit 1 wei, donate a huge amount so the
    /// victim's deposit rounds to (near) zero shares. With VIRTUAL_SHARE_OFFSET
    /// = 1000 the attack is massively unprofitable and the victim keeps >99%.
    function test_donationAttack_isUnprofitable() public {
        uint256 attackDeposit = 1;
        uint256 donation = 1e18;
        uint256 victimDeposit = 1e18;

        _fund(attacker, attackDeposit);
        _deposit(attacker, attackDeposit); // 1000 shares
        _donate(attacker, donation); // price now ~1e15 assets/share

        _fund(victim, victimDeposit);
        uint256 victimShares = _deposit(victim, victimDeposit);
        assertGt(victimShares, 0, "victim still gets shares");

        uint256 attackerOut = _redeem(attacker, vault.balanceOf(attacker));
        uint256 victimOut = _redeem(victim, victimShares);

        // Attacker spent 1 + 1e18 and recovers barely more than half of the
        // pool: the attack is a huge net loss.
        assertLt(attackerOut, attackDeposit + donation, "attacker cannot profit");
        assertLe(attackerOut * 100, (attackDeposit + donation) * 51, "attacker loses ~half the donation");
        // Victim's loss is bounded by roughly one share's value (~donation/1000).
        assertLe(victimDeposit - victimOut, donation / V + 2, "victim loss <= ~1 share");
        // Solvency: payouts never exceed holdings.
        assertLe(attackerOut + victimOut, attackDeposit + donation + victimDeposit);
    }

    /// Fuzzed version: for any donation size, the attacker ends with strictly
    /// less than they spent, and the victim's loss stays under ~1 share value.
    function testFuzz_donationAttack_neverProfitable(uint256 donation, uint256 victimDeposit) public {
        donation = bound(donation, 1e6, CAP);
        victimDeposit = bound(victimDeposit, donation / V, CAP);

        _fund(attacker, 1);
        _deposit(attacker, 1);
        _donate(attacker, donation);

        _fund(victim, victimDeposit);
        uint256 victimShares = _deposit(victim, victimDeposit);
        assertGt(victimShares, 0);

        uint256 attackerOut = _redeem(attacker, vault.balanceOf(attacker));
        uint256 victimOut = _redeem(victim, victimShares);

        assertLt(attackerOut, 1 + donation, "attacker net-negative");
        assertLe(victimDeposit - victimOut, (1 + donation) / V + 2, "victim loss <= ~1 share");
    }

    /// Donation to a live vault simply gifts every holder pro-rata.
    function test_donation_raisesPriceForHolders() public {
        _fund(alice, 1e18);
        _fund(bob, 1e18);
        _deposit(alice, 1e18);
        _deposit(bob, 1e18);

        _donate(attacker, 1e18);

        uint256 aliceOut = _redeem(alice, vault.balanceOf(alice));
        assertGt(aliceOut, 1e18, "donation accrues to holders");
    }

    /*//////////////////////////////////////////////////////////////
                            FULL REDEMPTION
    //////////////////////////////////////////////////////////////*/

    function testFuzz_fullRedeem_drainsVaultToDust(uint256 a, uint256 b) public {
        a = bound(a, 1, CAP);
        b = bound(b, 1, CAP);
        _fund(alice, a);
        _fund(bob, b);
        _deposit(alice, a);
        _deposit(bob, b);

        uint256 outA = _redeem(alice, vault.balanceOf(alice));
        uint256 outB = _redeem(bob, vault.balanceOf(bob));

        assertEq(vault.totalSupply(), 0, "all shares burned");
        assertLe(vault.totalAssets(), 2, "only sub-unit rounding dust remains");
        assertLe(outA, a);
        assertLe(outB, b);
        assertLe(outA + outB, a + b);
    }

    /*//////////////////////////////////////////////////////////////
                        MALICIOUS TOKEN CALLBACKS
    //////////////////////////////////////////////////////////////*/

    function test_reentrantToken_depositCallbackBlocked() public {
        ReentrantERC20 evil = new ReentrantERC20();
        ShareVault evilVault = new ShareVault(address(evil), "Evil Vault", "eMOCK");
        evil.setVault(evilVault);

        evil.mint(address(this), 1e18);
        evil.approve(address(evilVault), type(uint256).max);

        evil.armDeposit(1e18);
        uint256 shares = evilVault.deposit(1e18, address(this), 0);
        assertGt(shares, 0, "outer deposit succeeds");
        assertTrue(evil.reentryReverted(), "reentrant deposit reverted with Reentrancy");
        assertEq(evilVault.totalAssets(), 1e18, "pool state intact");
    }

    function test_reentrantToken_redeemCallbackBlocked() public {
        ReentrantERC20 evil = new ReentrantERC20();
        ShareVault evilVault = new ShareVault(address(evil), "Evil Vault", "eMOCK");
        evil.setVault(evilVault);

        evil.mint(address(this), 2e18);
        evil.approve(address(evilVault), type(uint256).max);
        uint256 shares = evilVault.deposit(1e18, address(this), 0);

        evil.armRedeem(shares / 2);
        uint256 out = evilVault.redeem(shares, address(this), 0);
        assertEq(out, 1e18, "outer redeem pays out fully");
        assertTrue(evil.reentryReverted(), "reentrant redeem reverted with Reentrancy");
        assertEq(evilVault.totalAssets(), 0);
    }

    /*//////////////////////////////////////////////////////////////
                              MAX SLIPPAGE
    //////////////////////////////////////////////////////////////*/

    function test_slippage_depositRespectsMinShares() public {
        _fund(alice, 1e18);
        uint256 quoted = vault.previewDeposit(1e18);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ShareVault.Slippage.selector, quoted, quoted + 1));
        vault.deposit(1e18, alice, quoted + 1);
    }

    function test_slippage_donationBetweenQuoteAndFill_reverts() public {
        _fund(alice, 1e18);
        _deposit(alice, 1e18); // seed the pool

        _fund(bob, 1e18);
        uint256 quoted = vault.previewDeposit(1e18);
        // Attacker moves the rate against bob between quote and execution.
        _donate(attacker, 1e18);
        uint256 worse = vault.previewDeposit(1e18);
        assertLt(worse, quoted);

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ShareVault.Slippage.selector, worse, quoted));
        vault.deposit(1e18, bob, quoted);
    }

    function test_slippage_redeemRespectsMinAssets() public {
        _fund(alice, 1e18);
        uint256 shares = _deposit(alice, 1e18);
        uint256 quoted = vault.previewRedeem(shares);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ShareVault.Slippage.selector, quoted, quoted + 1));
        vault.redeem(shares, alice, quoted + 1);
    }

    /*//////////////////////////////////////////////////////////////
                          FEE-ON-TRANSFER ASSET
    //////////////////////////////////////////////////////////////*/

    function test_feeOnTransfer_creditsReceivedAmount() public {
        FeeOnTransferERC20 feeToken = new FeeOnTransferERC20();
        ShareVault feeVault = new ShareVault(address(feeToken), "Fee Vault", "fMOCK");
        feeToken.mint(alice, 1000);
        vm.prank(alice);
        feeToken.approve(address(feeVault), type(uint256).max);

        vm.prank(alice);
        uint256 shares = feeVault.deposit(1000, alice, 0);
        assertEq(feeVault.totalAssets(), 990, "1% fee burned en route");
        assertEq(shares, 990 * V, "shares track assets actually received");
    }

    /*//////////////////////////////////////////////////////////////
                         PRICE MONOTONICITY (UNIT)
    //////////////////////////////////////////////////////////////*/

    function test_assetsPerShare_nonDecreasing() public {
        uint256 r0 = vault.assetsPerShare();
        _fund(alice, 1e24);
        _deposit(alice, 1e24);
        uint256 r1 = vault.assetsPerShare();
        _donate(attacker, 7e23);
        uint256 r2 = vault.assetsPerShare();
        _redeem(alice, vault.balanceOf(alice) / 3);
        uint256 r3 = vault.assetsPerShare();
        assertLe(r0, r1);
        assertLe(r1, r2);
        assertLe(r2, r3);
    }
}

contract Math512Harness {
    function mulDivDown(uint256 a, uint256 b, uint256 d) external pure returns (uint256) {
        return Math512.mulDivDown(a, b, d);
    }
}

contract Math512Test is Test {
    Math512Harness internal h = new Math512Harness();

    function test_mulDivDown_knownValues() public view {
        assertEq(h.mulDivDown(1e18, 1000, 1), 1000e18);
        assertEq(h.mulDivDown(7, 3, 2), 10); // floor(21/2)
        assertEq(h.mulDivDown(type(uint256).max, 1, 1), type(uint256).max);
        assertEq(h.mulDivDown(type(uint256).max, type(uint256).max, type(uint256).max), type(uint256).max);
        assertEq(h.mulDivDown(2 ** 255, 2, 2), 2 ** 255);
        assertEq(h.mulDivDown(5, 1, 3), 1); // floor(5/3)
    }

    function testFuzz_mulDivDown_matchesNaiveWhenNoOverflow(uint256 a, uint256 b, uint256 d) public view {
        d = bound(d, 1, type(uint256).max);
        b = bound(b, 0, type(uint256).max / (a == 0 ? 1 : a));
        assertEq(h.mulDivDown(a, b, d), (a * b) / d);
    }

    function test_mulDivDown_overflowReverts() public {
        vm.expectRevert(Math512.MulDivOverflow.selector);
        h.mulDivDown(2 ** 255, 2, 1);
    }

    function test_mulDivDown_divisionByZeroReverts() public {
        vm.expectRevert(Math512.DivisionByZero.selector);
        h.mulDivDown(1, 1, 0);
    }
}

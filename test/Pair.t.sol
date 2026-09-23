// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {CPMMFactory} from "../src/CPMMFactory.sol";
import {CPMMPair} from "../src/CPMMPair.sol";
import {CPMMMath} from "../src/CPMMMath.sol";
import {TestERC20} from "../src/mocks/TestERC20.sol";

/// @notice Unit tests for CPMMPair: exact fee math, reserves-vs-balances
///         invariant, MINIMUM_LIQUIDITY lock, share proportionality, and the
///         "no shares minted from thin air" property.
contract PairTest is Test {
    CPMMFactory internal factory;
    CPMMPair internal pair;
    TestERC20 internal tokenA;
    TestERC20 internal tokenB;

    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);

    uint256 internal constant WAD = 1e18;

    event Mint(address indexed sender, uint256 amount0, uint256 amount1);
    event Burn(address indexed sender, uint256 amount0, uint256 amount1, address indexed to);
    event Swap(
        address indexed sender,
        uint256 amount0In,
        uint256 amount1In,
        uint256 amount0Out,
        uint256 amount1Out,
        address indexed to
    );

    function setUp() public {
        factory = new CPMMFactory();
        tokenA = new TestERC20("A", "A");
        tokenB = new TestERC20("B", "");
        pair = CPMMPair(factory.createPair(address(tokenA), address(tokenB)));
        tokenA.mint(alice, 1_000_000 * WAD);
        tokenB.mint(alice, 1_000_000 * WAD);
        tokenA.mint(bob, 1_000_000 * WAD);
        tokenB.mint(bob, 1_000_000 * WAD);
    }

    // ------------------------------------------------------------------
    // Bootstrap + minimum liquidity
    // ------------------------------------------------------------------

    function test_FirstMint_LocksMinimumLiquidity() public {
        _mint(alice, 100 * WAD, 100 * WAD);
        // 100e18 - 1_000 shares to the user, 1_000 permanently at address(0).
        assertEq(pair.balanceOf(alice), 100 * WAD - 1_000);
        assertEq(pair.balanceOf(address(0)), 1_000);
        assertEq(pair.totalSupply(), 100 * WAD);
        // Locked shares can never be approved or moved (balanceOf zero address
        // has no key, and there is no owner).
        assertEq(pair.allowance(address(0), alice), 0);
    }

    function test_FirstMint_RevertsWhenLiquidityBelowMinimum() public {
        // sqrt(999*999) - 1000 < 0.
        vm.startPrank(alice);
        tokenA.transfer(address(pair), 999);
        tokenB.transfer(address(pair), 999);
        vm.expectRevert(CPMMPair.InsufficientFirstLiquidity.selector);
        pair.mint(alice, 999, 999);
        vm.stopPrank();
    }

    function test_ReservesMatchRealBalances_AfterEveryAction() public {
        _mint(alice, 100 * WAD, 100 * WAD);
        _assertReservesEqBalances();
        _mint(bob, 40 * WAD, 40 * WAD);
        _assertReservesEqBalances();
        _swap(bob, address(tokenA), 7 * WAD);
        _assertReservesEqBalances();
        _swap(alice, address(tokenB), 3 * WAD);
        _assertReservesEqBalances();
        _burn(bob, pair.balanceOf(bob) / 2);
        _assertReservesEqBalances();
        _burn(alice, pair.balanceOf(alice));
        _assertReservesEqBalances();
    }

    // ------------------------------------------------------------------
    // Fee: exactly 30 bps, applied before k
    // ------------------------------------------------------------------

    function test_Swap_ChargesExactly30Bps() public {
        _mint(alice, 1_000 * WAD, 1_000 * WAD);
        // Classic Uniswap numbers: 1e18 in against 1e21/1e21 reserves.
        uint256 ain = WAD;
        uint256 expected = CPMMMath.getAmountOut(ain, 1_000 * WAD, 1_000 * WAD);
        // Independent inline computation.
        uint256 independently = (ain * 997 * 1_000 * WAD) / (1_000 * WAD * 1000 + ain * 997);
        assertEq(expected, independently);

        uint256 got = _swapOut(bob, address(tokenA), ain);
        assertEq(got, expected);
        // No-fee output would be larger:
        uint256 noFee = (ain * 1_000 * WAD) / (1_000 * WAD + ain);
        assertGt(noFee, got, "fee must reduce payout");
        assertEq(noFee - got, noFee - expected);
    }

    function testFuzz_Swap_KNeverDecreases(uint256 ain) public {
        _mint(alice, 1_000 * WAD, 1_000 * WAD);
        // Lower bound chosen so the output is non-zero against 1_000e18
        // reserves (about 1_004 wei in); upper bound keeps liquidity ample.
        ain = bound(ain, 1_004, 100 * WAD);
        (uint112 r0Before, uint112 r1Before,) = pair.getReserves();
        uint256 kBefore = uint256(r0Before) * r1Before;
        _swapOut(bob, address(tokenA), ain);
        (uint112 r0After, uint112 r1After,) = pair.getReserves();
        uint256 kAfter = uint256(r0After) * r1After;
        // k measured in *unscaled* reserves strictly grows (fee stays in pool).
        assertGe(kAfter, kBefore, "k must not decrease");
    }

    function test_Swap_ZeroOutputReverts() public {
        _mint(alice, 1_000 * WAD, 1_000 * WAD);
        // 1 wei input against 1e21 reserves rounds to zero output.
        vm.prank(bob);
        tokenA.transfer(address(pair), 1);
        vm.prank(bob);
        vm.expectRevert(CPMMPair.InsufficientOutputAmount.selector);
        pair.swap(0, 0, bob, 1, 0, "");
    }

    function test_Swap_DrainsMoreThanReserveReverts() public {
        _mint(alice, 100 * WAD, 100 * WAD);
        vm.prank(bob);
        tokenA.transfer(address(pair), 1_000 * WAD);
        vm.prank(bob);
        vm.expectRevert(CPMMPair.InsufficientLiquidity.selector);
        pair.swap(0, 100 * WAD, bob, 1_000 * WAD, 0, ""); // output == reserve
    }

    function test_Swap_KInvariantRevertsOnUnderpayment() public {
        _mint(alice, 1_000 * WAD, 1_000 * WAD);
        // 100 tokens in have a fair output of ~90.66 tokens. Demanding 1.5x
        // the fair price still leaves ample reserves but violates x*y=k.
        uint256 ain = 100 * WAD;
        uint256 fair = CPMMMath.getAmountOut(ain, 1_000 * WAD, 1_000 * WAD);
        uint256 greedy = fair * 3 / 2;
        assertLt(greedy, 1_000 * WAD, "must not drain the reserve");
        vm.prank(bob);
        tokenA.transfer(address(pair), ain);
        bool aIs0 = address(tokenA) == address(pair.token0());
        vm.prank(bob);
        vm.expectRevert(CPMMPair.KInvariant.selector);
        if (aIs0) pair.swap(0, greedy, bob, ain, 0, "");
        else pair.swap(greedy, 0, bob, 0, ain, "");
    }

    function test_Swap_RejectsTokenAsRecipient() public {
        _mint(alice, 1_000 * WAD, 1_000 * WAD);
        vm.prank(bob);
        tokenA.transfer(address(pair), WAD);
        vm.prank(bob);
        vm.expectRevert(CPMMPair.InvalidTo.selector);
        pair.swap(0, 1e15, address(tokenA), WAD, 0, "");
    }

    // ------------------------------------------------------------------
    // Share proportionality on add/remove
    // ------------------------------------------------------------------

    function test_AddRemoveLiquidity_MaintainsProportion() public {
        _mint(alice, 100 * WAD, 100 * WAD);

        // Pool supply is 100e18 (1_000 locked + Alice's ~100e18-1_000).
        // Bob deposits 25 each against 100/100 reserves -> 25e18 shares,
        // which is 20% of the post-deposit supply (125e18).
        _mint(bob, 25 * WAD, 25 * WAD);
        uint256 total = pair.totalSupply();
        assertEq(pair.balanceOf(bob) * 1e18 / total, 0.2e18, "bob 20%");

        (uint112 r0, uint112 r1,) = pair.getReserves();
        uint256 bobShares = pair.balanceOf(bob);

        // Bob burns half his shares: he must receive half of the reserves his
        // full position is entitled to (floor each side).
        uint256 burnShares = bobShares / 2;
        (uint256 got0, uint256 got1) = _burn(bob, burnShares);
        uint256 exp0 = burnShares * uint256(r0) / total;
        uint256 exp1 = burnShares * uint256(r1) / total;
        assertEq(got0, exp0, "token0 pro-rata");
        assertEq(got1, exp1, "token1 pro-rata");
    }

    function test_SharesCannotBeMintedFromThinAir() public {
        _mint(alice, 100 * WAD, 100 * WAD);
        uint256 supplyBefore = pair.totalSupply();

        // Direct token donations must NOT mint shares; sync only moves reserves
        // and skim sends the excess away.
        tokenA.mint(address(this), 15 * WAD);
        tokenB.mint(address(this), 15 * WAD);
        tokenA.transfer(address(pair), 10 * WAD);
        tokenB.transfer(address(pair), 10 * WAD);
        // No mint call -> supply unchanged.
        assertEq(pair.totalSupply(), supplyBefore, "donation must not mint");
        pair.skim(alice);
        assertEq(pair.totalSupply(), supplyBefore, "skim must not mint");

        // sync absorbs donations into reserves but still cannot mint.
        tokenA.transfer(address(pair), 5 * WAD);
        tokenB.transfer(address(pair), 5 * WAD);
        pair.sync();
        assertEq(pair.totalSupply(), supplyBefore, "sync must not mint");
        (uint112 r0, uint112 r1,) = pair.getReserves();
        assertEq(uint256(r0), 105 * WAD);
        assertEq(uint256(r1), 105 * WAD);
    }

    function test_SecondMintRoundsDown_NeverOverpaysShares() public {
        _mint(alice, 100 * WAD, 100 * WAD);
        // Slightly imbalanced deposit: the min-ratio rule caps the payout.
        uint256 sharesBefore = pair.balanceOf(bob);
        _mint(bob, 30 * WAD, 31 * WAD);
        uint256 minted = pair.balanceOf(bob) - sharesBefore;
        assertEq(minted, 30 * WAD, "payout equals the binding (smaller) ratio");
    }

    // ------------------------------------------------------------------
    // Factory / token ordering
    // ------------------------------------------------------------------

    function test_Factory_OrdersTokensAndIndexesBothWays() public {
        address lookup1 = factory.getPair(address(tokenA), address(tokenB));
        address lookup2 = factory.getPair(address(tokenB), address(tokenA));
        assertEq(lookup1, address(pair));
        assertEq(lookup2, address(pair));
        assertEq(factory.allPairsLength(), 1);
        // token0 is the numerically smaller address.
        (address t0, address t1) =
            address(tokenA) < address(tokenB) ? (address(tokenA), address(tokenB)) : (address(tokenB), address(tokenA));
        assertEq(address(pair.token0()), t0);
        assertEq(address(pair.token1()), t1);
    }

    function test_Factory_DuplicatePairReverts() public {
        vm.expectRevert(CPMMFactory.PairExists.selector);
        factory.createPair(address(tokenA), address(tokenB));
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    function _mint(address who, uint256 a, uint256 b) internal {
        vm.startPrank(who);
        tokenA.transfer(address(pair), a);
        tokenB.transfer(address(pair), b);
        // expected amounts must be passed in token0/token1 order.
        if (address(tokenA) == address(pair.token0())) {
            pair.mint(who, a, b);
        } else {
            pair.mint(who, b, a);
        }
        vm.stopPrank();
    }

    function _swap(address who, address tIn, uint256 amountIn) internal {
        _swapOut(who, tIn, amountIn);
    }

    function _swapOut(address who, address tIn, uint256 amountIn) internal returns (uint256 out) {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        bool isA = tIn == address(tokenA);
        out = CPMMMath.getAmountOut(amountIn, isA ? uint256(r0) : uint256(r1), isA ? uint256(r1) : uint256(r0));
        vm.startPrank(who);
        TestERC20(tIn).transfer(address(pair), amountIn);
        if (isA) pair.swap(0, out, who, amountIn, 0, "");
        else pair.swap(out, 0, who, 0, amountIn, "");
        vm.stopPrank();
    }

    function _burn(address who, uint256 shares) internal returns (uint256 gotA, uint256 gotB) {
        uint256 beforeA = tokenA.balanceOf(who);
        uint256 beforeB = tokenB.balanceOf(who);
        vm.startPrank(who);
        pair.transfer(address(pair), shares);
        pair.burn(who);
        vm.stopPrank();
        gotA = tokenA.balanceOf(who) - beforeA;
        gotB = tokenB.balanceOf(who) - beforeB;
    }

    function _assertReservesEqBalances() internal view {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        TestERC20 t0 = TestERC20(address(pair.token0()));
        TestERC20 t1 = TestERC20(address(pair.token1()));
        assertEq(t0.balanceOf(address(pair)), uint256(r0), "bal0==reserve0");
        assertEq(t1.balanceOf(address(pair)), uint256(r1), "bal1==reserve1");
    }
}

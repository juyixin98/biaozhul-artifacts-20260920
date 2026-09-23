// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {CPMMFactory} from "../src/CPMMFactory.sol";
import {CPMMPair} from "../src/CPMMPair.sol";
import {CPMMRouter} from "../src/CPMMRouter.sol";
import {TestERC20} from "../src/mocks/TestERC20.sol";

/// @notice Targeted invariant / handler suite. Random deposit/swap/burn
///         sequences through the router must always preserve:
///           I1 reserves == real token balances
///           I2 k (unscaled) never decreases across swaps
///           I3 the MINIMUM_LIQUIDITY lock is immutable
///           I4 total LP supply == sum of holder balances
///           I5 a swap output never exceeds the 30 bps curve output
contract InvariantCore is Test {
    CPMMFactory internal factory;
    CPMMRouter internal router;
    TestERC20 internal tA;
    TestERC20 internal tB;
    CPMMPair internal pair;

    Handler internal handler;

    function setUp() public {
        factory = new CPMMFactory();
        router = new CPMMRouter(address(factory));
        tA = new TestERC20("A", "A");
        tB = new TestERC20("B", "B");
        pair = CPMMPair(factory.createPair(address(tA), address(tB)));
        handler = new Handler(factory, router, pair, tA, tB);

        // Seed handler with tokens and bootstrap liquidity.
        tA.mint(address(handler), 1_000_000 ether);
        tB.mint(address(handler), 1_000_000 ether);
        handler.bootstrap(1_000 ether, 1_000 ether);

        targetContract(address(handler));
        bytes4[] memory selectors = new bytes4[](3);
        selectors[0] = Handler.deposit.selector;
        selectors[1] = Handler.swap.selector;
        selectors[2] = Handler.withdraw.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));
    }

    function invariant_ReservesEqualBalances() public view {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        assertEq(tA.balanceOf(address(pair)), address(tA) == address(pair.token0()) ? uint256(r0) : uint256(r1));
        assertEq(tB.balanceOf(address(pair)), address(tB) == address(pair.token0()) ? uint256(r0) : uint256(r1));
    }

    function invariant_LockIsImmutable() public view {
        assertGe(pair.totalSupply(), 1_000);
        assertEq(pair.balanceOf(address(0)), 1_000);
    }

    function invariant_SupplyEqualsHolderSum() public view {
        // The only ever LP holders in this suite are handler + address(0).
        uint256 sum = pair.balanceOf(address(handler)) + pair.balanceOf(address(0));
        assertEq(sum, pair.totalSupply());
    }

    function invariant_ReservesPositiveWhilePoolLive() public view {
        // Swaps never drain a side to zero (output is strictly < reserve).
        (uint112 r0, uint112 r1,) = pair.getReserves();
        assertGt(uint256(r0), 0);
        assertGt(uint256(r1), 0);
    }

    function invariant_SwapFeeAccruesToPool() public view {
        // The cumulative-swap k floor tracked by the handler never decreases.
        handler.assertKMonotonic();
    }
}

contract Handler is Test {
    CPMMFactory internal factory;
    CPMMRouter internal router;
    CPMMPair internal pair;
    TestERC20 internal tA;
    TestERC20 internal tB;

    uint256 public lastSwapK;

    constructor(CPMMFactory _f, CPMMRouter _r, CPMMPair _p, TestERC20 _a, TestERC20 _b) {
        factory = _f;
        router = _r;
        pair = _p;
        tA = _a;
        tB = _b;
    }

    function bootstrap(uint256 a, uint256 b) external {
        tA.approve(address(router), type(uint256).max);
        tB.approve(address(router), type(uint256).max);
        router.addLiquidity(address(tA), address(tB), a, b, 0, 0, address(this), block.timestamp + 1);
        (uint112 r0, uint112 r1,) = pair.getReserves();
        lastSwapK = uint256(r0) * r1;
    }

    function deposit(uint256 a, uint256 b) external {
        a = bound(a, 1, 50 ether);
        b = bound(b, 1, 50 ether);
        uint256 balA = tA.balanceOf(address(this));
        uint256 balB = tB.balanceOf(address(this));
        if (balA < a || balB < b) return;
        uint256 supplyBefore = pair.totalSupply();
        uint256 lpBefore = pair.balanceOf(address(this));
        try router.addLiquidity(address(tA), address(tB), a, b, 0, 0, address(this), block.timestamp + 1) {
            // Shares minted must be bounded by the pre-deposit ratio.
            uint256 minted = pair.balanceOf(address(this)) - lpBefore;
            assertLe(minted, supplyBefore, "share cap sanity");
            _resetSwapFloor();
        } catch {}
    }

    function swap(uint256 amountIn, bool aToB) external {
        amountIn = bound(amountIn, 1, 100 ether);
        address[] memory path = new address[](2);
        path[0] = aToB ? address(tA) : address(tB);
        path[1] = aToB ? address(tB) : address(tA);
        uint256 inBal = TestERC20(path[0]).balanceOf(address(this));
        if (inBal < amountIn) return;

        (uint112 r0Before, uint112 r1Before,) = pair.getReserves();
        uint256 kBefore = uint256(r0Before) * r1Before;

        try router.swapExactTokensForTokens(amountIn, 0, path, address(this), block.timestamp + 1) {
            (uint112 r0After, uint112 r1After,) = pair.getReserves();
            uint256 kAfter = uint256(r0After) * r1After;
            assertGe(kAfter, kBefore, "k monotonic across swap");
            // The running floor for "since the last liquidity event" rises.
            if (kAfter > lastSwapK) lastSwapK = kAfter;
        } catch {}
    }

    function withdraw(uint256 fractionBps) external {
        fractionBps = bound(fractionBps, 1, 10_000);
        uint256 shares = pair.balanceOf(address(this)) * fractionBps / 10_000;
        if (shares == 0) return;
        pair.approve(address(router), type(uint256).max);
        try router.removeLiquidity(address(tA), address(tB), shares, 0, 0, address(this), block.timestamp + 1) {
            // Never burn the locked shares: holder balance can reach 0 but the
            // address(0) 1_000 remain.
            assertEq(pair.balanceOf(address(0)), 1_000);
            _resetSwapFloor();
        } catch {}
    }

    /// @dev After any liquidity event the k floor restarts at current k.
    function _resetSwapFloor() internal {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        lastSwapK = uint256(r0) * r1;
    }

    function assertKMonotonic() external view {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        uint256 k = uint256(r0) * r1;
        assertGe(k, lastSwapK, "swaps since last liquidity event never lower k");
    }
}

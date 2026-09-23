// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ConstantProductPool} from "../src/ConstantProductPool.sol";
import {TestERC20} from "../src/test/TestERC20.sol";

/// @title PoolHandler
/// @notice Bounded action surface for invariant fuzzing. Every action is a
///         legitimate pool call (swap/add/remove/sync/transfer-shares); the
///         handler keeps track of expected aggregate quantities.
contract PoolHandler is Test {
    ConstantProductPool public pool;
    TestERC20 public t0;
    TestERC20 public t1;

    address[] public actors;
    uint256 public actorCount = 4;

    // ghost variables
    uint256 public ghostSumShares; // shares held by all tracked actors + lock
    uint256 public ghostSwapCount;
    uint256 public ghostKPrev;

    constructor(ConstantProductPool _pool, TestERC20 _t0, TestERC20 _t1) {
        pool = _pool;
        t0 = _t0;
        t1 = _t1;
        for (uint256 i = 0; i < actorCount; i++) {
            address a = address(uint160(uint256(keccak256(abi.encode("actor", i)))));
            actors.push(a);
            t0.mint(a, 1_000_000e18);
            t1.mint(a, 1_000_000e18);
            vm.prank(a);
            t0.approve(address(pool), type(uint256).max);
            vm.prank(a);
            t1.approve(address(pool), type(uint256).max);
        }
        // seed
        address lp = actors[0];
        vm.prank(lp);
        pool.addLiquidity(1000e18, 1000e18, 0, lp, type(uint256).max);
        ghostSumShares = pool.totalSupply();
        ghostKPrev = 1000e18 * 1000e18;
    }

    function _actor(uint256 seed) internal view returns (address) {
        return actors[seed % actorCount];
    }

    function swap(uint256 seedActor, uint256 amount, bool zeroForOne) external {
        amount = bound(amount, 1, 50e18);
        address a = _actor(seedActor);
        address tin = zeroForOne ? address(t0) : address(t1);
        try pool.quoteAmountOut(tin, amount) returns (uint256 out) {
            if (out == 0) return;
            (uint112 r0b, uint112 r1b,) = pool.getReserves();
            uint256 kBefore = uint256(r0b) * uint256(r1b);
            vm.prank(a);
            try pool.swapExactInput(tin, amount, 0, a, type(uint256).max) {
                (uint112 r0a, uint112 r1a,) = pool.getReserves();
                // fee ensures k never decreases
                assertGe(uint256(r0a) * uint256(r1a), kBefore, "k monotonic");
                ghostSwapCount++;
            } catch {}
        } catch {}
    }

    function add(uint256 seedActor, uint256 d0, uint256 d1) external {
        d0 = bound(d0, 1e6, 50e18);
        d1 = bound(d1, 1e6, 50e18);
        address a = _actor(seedActor);
        vm.prank(a);
        try pool.addLiquidity(d0, d1, 0, a, type(uint256).max) {
            ghostSumShares = pool.totalSupply();
        } catch {}
    }

    function remove(uint256 seedActor, uint256 pct) external {
        address a = _actor(seedActor);
        uint256 bal = pool.balanceOf(a);
        if (bal == 0) return;
        uint256 shares = bound(pct, 1, bal);
        vm.prank(a);
        try pool.removeLiquidity(shares, 0, 0, a, type(uint256).max) {
            ghostSumShares = pool.totalSupply();
        } catch {}
    }

    function moveShares(uint256 seedFrom, uint256 seedTo, uint256 amount) external {
        address from = _actor(seedFrom);
        address to = _actor(seedTo);
        uint256 bal = pool.balanceOf(from);
        if (bal == 0) return;
        amount = bound(amount, 1, bal);
        vm.prank(from);
        try pool.transfer(to, amount) {} catch {}
    }

    function callSummary() external view returns (uint256) {
        return ghostSwapCount;
    }
}

contract PoolInvariantTest is Test {
    ConstantProductPool internal pool;
    TestERC20 internal t0;
    TestERC20 internal t1;
    PoolHandler internal handler;

    address[] internal actors;

    function setUp() public {
        t0 = new TestERC20("T0", "T0");
        t1 = new TestERC20("T1", "T1");
        pool = new ConstantProductPool(address(t0), address(t1));
        handler = new PoolHandler(pool, t0, t1);
        for (uint256 i = 0; i < handler.actorCount(); i++) {
            actors.push(handler.actors(i));
        }

        // Only the handler may touch the pool; direct token transfers to it
        // are excluded here (they are a distinct, explicitly tested case).
        targetContract(address(handler));
        bytes4[] memory sel = new bytes4[](4);
        sel[0] = PoolHandler.swap.selector;
        sel[1] = PoolHandler.add.selector;
        sel[2] = PoolHandler.remove.selector;
        sel[3] = PoolHandler.moveShares.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sel}));
    }

    /// @dev Invariant 1 (the headline property): reserves exactly equal the
    ///      pool's real token balances after every call sequence.
    function invariant_ReservesEqualBalances() public view {
        (uint112 r0, uint112 r1,) = pool.getReserves();
        assertEq(t0.balanceOf(address(pool)), uint256(r0), "r0==bal0");
        assertEq(t1.balanceOf(address(pool)), uint256(r1), "r1==bal1");
    }

    /// @dev Invariant 2: total shares == sum of all balances, including the
    ///      permanent 1000-share lock at address(0). Shares never appear
    ///      from nothing and never disappear except via burn.
    function invariant_ShareSupplyConservation() public view {
        uint256 sum = pool.balanceOf(address(0));
        for (uint256 i = 0; i < actors.length; i++) {
            sum += pool.balanceOf(actors[i]);
        }
        assertEq(sum, pool.totalSupply(), "sum(balances)==supply");
        assertEq(pool.balanceOf(address(0)), 1000, "lock exactly 1000");
    }

    /// @dev Invariant 3: tracked actors collectively own all shares outside
    ///      the lock (no phantom holders created by pool operations).
    function invariant_NoPhantomHolders() public view {
        uint256 held;
        for (uint256 i = 0; i < actors.length; i++) {
            held += pool.balanceOf(actors[i]);
        }
        assertEq(held + 1000, pool.totalSupply());
    }

    /// @dev Invariant 4: the pool never holds more of either token than the
    ///      total that was seeded (no token minted into reserves), and
    ///      balances never underflow.
    function invariant_BalancesBounded() public view {
        // 4 actors * 1,000,000 + same for t1 = 4,000,000 e18 max funded
        assertLe(t0.balanceOf(address(pool)), 4_000_001e18);
        assertLe(t1.balanceOf(address(pool)), 4_000_001e18);
    }

    /// @dev Invariant 5: every LP share is backed by non-zero tokens on both
    ///      sides once liquidity exists (redeemable value is positive).
    function invariant_SharesBacked() public view {
        uint256 ts = pool.totalSupply();
        if (ts == 0) return;
        (uint112 r0, uint112 r1,) = pool.getReserves();
        assertGt(uint256(r0), 0);
        assertGt(uint256(r1), 0);
        // one share's redemption slice cannot be zero on both sides
        assertTrue((uint256(r0) * 1) / ts > 0 || (uint256(r1) * 1) / ts > 0);
    }
}

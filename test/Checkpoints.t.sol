// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Checkpoints} from "../src/Checkpoints.sol";

/// @dev Minimal cheatcode interface — no forge-std needed.
interface Vm {
    function roll(uint256 newHeight) external;
    function expectRevert(bytes calldata data) external;
    function startPrank(address msgSender) external;
    function stopPrank() external;
}

/// @notice Pure-Solidity acceptance tests + gas benchmarks for Checkpoints.
/// The gas table is printed via events (no test framework dependency).
contract CheckpointsTest {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    event GasTable(uint256 checkpointCount, string op, uint256 gasUsed);

    Checkpoints internal cp;

    function setUp() external {
        cp = new Checkpoints();
        // Deterministic starting height: block 1.
        vm.roll(1);
    }

    // ---------- functional cases ----------

    /// Empty history: no checkpoint exists at any past block.
    function test_EmptyHistory() external {
        (bool ex,,) = cp.latest();
        require(!ex, "latest should be empty");

        vm.roll(100);
        (ex,,) = cp.getAtBlock(0);
        require(!ex, "block 0 must be empty");
        (ex,,) = cp.getAtBlock(1);
        require(!ex, "block 1 must be empty");
        (ex,,) = cp.getAtBlock(100);
        require(!ex, "block 100 must be empty");
    }

    /// First block: a single checkpoint, queried exactly and via gaps.
    function test_FirstBlock() external {
        cp.setValue(42);
        require(cp.length() == 1, "length 1");

        vm.roll(3);
        (bool ex, uint256 bn, uint256 v) = cp.getAtBlock(3);
        require(ex && bn == 1 && v == 42, "query after gap returns first cp");

        // Target exactly on the checkpoint block.
        (ex, bn, v) = cp.getAtBlock(1);
        require(ex && bn == 1 && v == 42, "exact block returns first cp");

        // Target before the first checkpoint -> nothing.
        // (block 0 query allowed since 0 <= block.number=3)
        (ex,,) = cp.getAtBlock(0);
        require(!ex, "pre-history target must be empty");
    }

    /// Gaps between checkpoint blocks: lookup returns the last value before
    /// the target, not the next one.
    function test_BlockGaps() external {
        vm.roll(10);
        cp.setValue(100);
        vm.roll(20);
        cp.setValue(200);
        vm.roll(30);
        cp.setValue(300);
        require(cp.length() == 3, "three checkpoints");

        _expect(9, false, 0, 0);   // before first
        _expect(10, true, 10, 100);
        _expect(11, true, 10, 100);
        _expect(19, true, 10, 100);
        _expect(20, true, 20, 200);
        _expect(25, true, 20, 200); // middle of a wide gap
        _expect(29, true, 20, 200);
        _expect(30, true, 30, 300);

        // Current block carries the last value.
        (bool ex,, uint256 v) = cp.latest();
        require(ex && v == 300, "latest = 300");
    }

    /// Many updates in the same block collapse into a single checkpoint.
    function test_ManyUpdatesSameBlock() external {
        vm.roll(7);
        for (uint256 i = 1; i <= 500; i++) {
            cp.setValue(i * 10);
        }
        require(cp.length() == 1, "same-block updates must merge");

        (uint64 bn, uint256 v) = cp.checkpointAt(0);
        require(bn == 7 && v == 5000, "last write wins");

        // Later block still sees the merged value.
        vm.roll(8);
        (bool ex,, uint256 gv) = cp.getAtBlock(8);
        require(ex && gv == 5000, "value survives into the next block");
    }

    /// After a merge, a new block appends a fresh checkpoint.
    function test_MergeThenAppend() external {
        vm.roll(5);
        cp.setValue(1);
        cp.setValue(2);
        cp.setValue(3);
        require(cp.length() == 1, "merged");
        vm.roll(6);
        cp.setValue(4);
        require(cp.length() == 2, "appended next block");
        _expect(5, true, 5, 3);
        _expect(6, true, 6, 4);
    }

    /// Future-block queries must revert, both before and after writes.
    function test_RejectsFutureBlock() external {
        // Full ABI encoding of Checkpoints.FutureBlock(11, 10).
        bytes memory futureBlockData = abi.encodeWithSignature(
            "FutureBlock(uint256,uint256)", 11, 10
        );
        vm.roll(10);
        vm.expectRevert(futureBlockData);
        cp.getAtBlock(11);

        cp.setValue(1);
        vm.expectRevert(futureBlockData);
        cp.getAtBlock(11);

        // Equal to current block is allowed.
        (bool ex,,) = cp.getAtBlock(10);
        require(ex, "current block must be queryable");
    }

    function _expect(
        uint256 target,
        bool wantExists,
        uint256 wantBlock,
        uint256 wantValue
    ) internal view {
        (bool ex, uint256 bn, uint256 v) = cp.getAtBlock(target);
        require(ex == wantExists, "exists mismatch");
        if (wantExists) {
            require(bn == wantBlock, "block mismatch");
            require(v == wantValue, "value mismatch");
        }
    }

    // ---------- gas benchmarks ----------

    /// Writes: a merged same-block update must cost materially less than an
    /// append, and append cost must stay roughly flat as history grows
    /// (no linear scan on write — this would fail a linear implementation).
    function testGas_Writes() external {
        vm.roll(1);
        // First-ever append (SSTORE 0->value, also grows the array).
        uint256 firstAppend = _set(7);
        emit GasTable(0, "append_first", firstAppend);

        // Warm append baseline: new block, second checkpoint ever.
        vm.roll(2);
        uint256 appendAt2 = _set(8);
        emit GasTable(1, "append", appendAt2);

        // Merged update still at block 2.
        uint256 merged = _set(9);
        emit GasTable(2, "merge_same_block", merged);
        require(merged < appendAt2, "merge must be cheaper than append");

        // Grow history to 200 blocks, measuring append cost at the end.
        for (uint256 b = 3; b <= 200; b++) {
            vm.roll(b);
            if (b < 200) cp.setValue(b);
        }
        uint256 appendAt200 = _set(200);
        emit GasTable(199, "append", appendAt200);

        // An append touches only the tail slot: growth from n=2 to n=200
        // must be negligible (well under one extra cold SLOAD worth).
        uint256 diff = appendAt200 > appendAt2
            ? appendAt200 - appendAt2
            : appendAt2 - appendAt200;
        require(diff < 6_000, "append cost must not grow with history length");

        // A merge must remain cheaper even with long history.
        uint256 mergedAt200 = _set(201);
        emit GasTable(200, "merge_same_block", mergedAt200);
        require(
            mergedAt200 * 100 < appendAt200 * 60,
            "merge must cost <60% of append"
        );

        // Sanity: first append is expected to be the priciest (cold slots,
        // length init). It must be within a fixed bound regardless of total
        // history by construction — record it for the report only.
        require(firstAppend > 0, "gas nonzero");
    }

    /// Reads: build independent histories of n = 2^k checkpoints and measure
    /// a worst-position lookup (target at/after the latest block so the
    /// binary search walks every level, all slots cold in a fresh contract).
    function testGas_Reads_LogGrowth() external {
        uint256[] memory sizes = new uint256[](5);
        sizes[0] = 4;
        sizes[1] = 16;
        sizes[2] = 64;
        sizes[3] = 256;
        sizes[4] = 1024;

        uint256[] memory costs = new uint256[](5);
        for (uint256 k = 0; k < sizes.length; k++) {
            Checkpoints fresh = new Checkpoints();
            for (uint256 b = 1; b <= sizes[k]; b++) {
                vm.roll(b);
                fresh.setValue(b * 3);
            }
            // Query the latest block: cold slots, full search depth.
            costs[k] = _getGas(fresh, sizes[k]);
            emit GasTable(sizes[k], "lookup_latest_cold", costs[k]);
        }

        // Logarithmic claim: doubling history must add near-constant gas
        // (one extra probe). Allow generous bounds so the assertion tests
        // O(log n) vs O(n) rather than a fragile exact figure.
        for (uint256 k = 1; k < costs.length; k++) {
            require(
                costs[k] > costs[k - 1],
                "more probes should not cost less on average"
            );
            // One extra binary-search probe (an SLOAD ~ 2.1k cold plus
            // overhead): a linear scan would add thousands here.
            require(
                costs[k] - costs[k - 1] < 9_000,
                "per-doubling lookup gas must stay bounded (log n)"
            );
        }

        // Absolute bound at 1024 checkpoints: a linear scan of cold slots
        // would cost > 2m gas; a log search is a few tens of thousands.
        require(costs[4] < 80_000, "1024-entry lookup must be ~logarithmic");
    }

    function _set(uint256 value) internal returns (uint256 used) {
        uint256 beforeGas = gasleft();
        cp.setValue(value);
        used = beforeGas - gasleft();
    }

    function _getGas(Checkpoints c, uint256 target)
        internal
        view
        returns (uint256 used)
    {
        uint256 beforeGas = gasleft();
        c.getAtBlock(target);
        used = beforeGas - gasleft();
    }
}

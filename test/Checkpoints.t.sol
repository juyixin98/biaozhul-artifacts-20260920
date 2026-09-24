// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Checkpoints} from "../src/Checkpoints.sol";
import {NaiveCheckpoints} from "../src/NaiveCheckpoints.sol";

contract CheckpointsTest is Test {
    Checkpoints internal cp;
    NaiveCheckpoints internal naive;

    event CheckpointUpdated(uint256 indexed blockNumber, uint256 value);

    function setUp() public {
        cp = new Checkpoints();
        naive = new NaiveCheckpoints();
    }

    // ------------------------------------------------------------------
    // 场景 1：空记录
    // ------------------------------------------------------------------

    function test_EmptyRecord_QueriesReturnZero() public view {
        assertEq(cp.length(), 0);
        (uint256 b, uint256 v) = cp.latest();
        assertEq(b, 0);
        assertEq(v, 0);
        assertEq(cp.valueAt(0), 0);
        assertEq(cp.valueAt(1), 0);
        assertEq(cp.valueAt(block.number), 0);
    }

    // ------------------------------------------------------------------
    // 场景 2：第一块
    // ------------------------------------------------------------------

    function test_FirstCheckpoint() public {
        vm.roll(100);
        cp.setValue(42);
        assertEq(cp.length(), 1);

        // 早于第一个检查点：没有值
        assertEq(cp.valueAt(0), 0);
        assertEq(cp.valueAt(99), 0);
        // 恰好第一块：取到值
        assertEq(cp.valueAt(100), 42);
        // 之后没有新检查点：值持续有效
        vm.roll(101);
        assertEq(cp.valueAt(101), 42);
        vm.roll(150);
        assertEq(cp.valueAt(150), 42);
    }

    // ------------------------------------------------------------------
    // 场景 3：块间空隙
    // ------------------------------------------------------------------

    function test_GapsBetweenBlocks() public {
        vm.roll(10);
        cp.setValue(1);
        vm.roll(20);
        cp.setValue(2);
        vm.roll(50);
        cp.setValue(3);
        assertEq(cp.length(), 3);

        // 空隙中查询返回空隙之前的最后值
        assertEq(cp.valueAt(0), 0);
        assertEq(cp.valueAt(9), 0);
        assertEq(cp.valueAt(10), 1);
        assertEq(cp.valueAt(11), 1);
        assertEq(cp.valueAt(19), 1);
        assertEq(cp.valueAt(20), 2);
        assertEq(cp.valueAt(21), 2);
        assertEq(cp.valueAt(49), 2);
        assertEq(cp.valueAt(50), 3);
        // 后续区块（先推进区块再查询）
        vm.roll(60);
        assertEq(cp.valueAt(51), 3);
        assertEq(cp.valueAt(60), 3);
        vm.roll(1_000_000);
        assertEq(cp.valueAt(1_000_000), 3);
    }

    // ------------------------------------------------------------------
    // 场景 4：大量同块更新合并
    // ------------------------------------------------------------------

    function test_ManyUpdatesSameBlock_Merge() public {
        vm.roll(321);
        for (uint256 i = 1; i <= 500; i++) {
            cp.setValue(i);
        }
        // 同区块无论更新多少次，只保留一个检查点，值为最后一次
        assertEq(cp.length(), 1);
        assertEq(cp.valueAt(321), 500);

        (uint256 b, uint256 v) = cp.latest();
        assertEq(b, 321);
        assertEq(v, 500);

        // 下一个区块再更新，产生第二个检查点
        vm.roll(322);
        cp.setValue(7);
        assertEq(cp.length(), 2);
        assertEq(cp.valueAt(321), 500);
        assertEq(cp.valueAt(322), 7);

        // 再回到「上一区块语义」不会发生：区块号单调，合并只看当前块
        (, uint256 latestValue) = cp.latest();
        assertEq(latestValue, 7);
    }

    function test_SameBlockMerge_EmitsEventPerUpdate() public {
        vm.roll(7);
        // 同块两次写入各发出事件，但最终只保留一个检查点
        vm.expectEmit(true, false, false, true);
        emit CheckpointUpdated(7, 1);
        cp.setValue(1);
        vm.expectEmit(true, false, false, true);
        emit CheckpointUpdated(7, 99);
        cp.setValue(99);
        assertEq(cp.length(), 1);
    }

    // ------------------------------------------------------------------
    // 拒绝未来块查询
    // ------------------------------------------------------------------

    function test_RevertWhen_QueryFutureBlock_Empty() public {
        vm.expectRevert(abi.encodeWithSelector(Checkpoints.FutureBlock.selector, 101, 1));
        cp.valueAt(101);
    }

    function test_RevertWhen_QueryFutureBlock_WithHistory() public {
        vm.roll(100);
        cp.setValue(1);
        // 当前块 100，查询 101 必须回滚
        vm.expectRevert(abi.encodeWithSelector(Checkpoints.FutureBlock.selector, 101, 100));
        cp.valueAt(101);
        // 当前块本身合法
        assertEq(cp.valueAt(100), 1);
    }

    // ------------------------------------------------------------------
    // 权限与边界
    // ------------------------------------------------------------------

    function test_RevertWhen_NonOwnerSetsValue() public {
        vm.prank(address(0xBEEF));
        vm.expectRevert(Checkpoints.NotOwner.selector);
        cp.setValue(1);
    }

    function test_RevertWhen_ValueExceedsUint224() public {
        uint256 tooBig = uint256(type(uint224).max) + 1;
        vm.expectRevert(abi.encodeWithSelector(Checkpoints.ValueTooLarge.selector, tooBig));
        cp.setValue(tooBig);
    }

    function test_MaxUint224Accepted() public {
        cp.setValue(type(uint224).max);
        assertEq(cp.valueAt(block.number), type(uint224).max);
    }

    // ------------------------------------------------------------------
    // 模糊测试：随机历史下二分实现 == 线性扫描参考实现
    // ------------------------------------------------------------------

    /// @notice 对随机操作序列（同块/跨块写入）构建两份历史，
    ///         再对一批随机目标块比较两个合约的查询结果。
    function testFuzz_BinaryMatchesLinear(
        uint8 opCount,
        uint256 seed
    ) public {
        uint256 n = uint256(opCount) % 64; // 0..63 次写入
        uint256 rng = seed == 0 ? 1 : seed;

        for (uint256 i = 0; i < n; i++) {
            // 伪随机推进 0..3 个区块，制造同块合并与空隙
            rng = _nextRng(rng);
            uint256 delta = rng % 4;
            vm.roll(block.number + delta);

            rng = _nextRng(rng);
            uint256 value = rng % 1000;
            cp.setValue(value);
            naive.setValue(value);
        }

        assertEq(cp.length(), naive.length());

        // 用不同目标块查询：第 0 块、当前块、以及中间随机块
        rng = _nextRng(rng);
        uint256 cur = block.number;
        uint256[] memory targets = new uint256[](10);
        targets[0] = 0;
        targets[1] = cur;
        if (cur > 0) {
            targets[2] = cur - 1;
            targets[3] = cur / 2;
        }
        for (uint256 k = 4; k < 10; k++) {
            rng = _nextRng(rng);
            targets[k] = rng % (cur + 1);
        }
        for (uint256 k = 0; k < targets.length; k++) {
            assertEq(cp.valueAt(targets[k]), naive.valueAt(targets[k]), "query mismatch");
        }
    }

    function _nextRng(uint256 x) private pure returns (uint256) {
        // xorshift32 风格的确定性伪随机
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        return x;
    }

    // ------------------------------------------------------------------
    // gas：二分 vs 线性，随历史长度增长
    // ------------------------------------------------------------------

    function test_Gas_BinaryVsLinear_AtOldestTarget() public {
        uint256 n = 256; // 256 个不同区块的检查点
        for (uint256 i = 1; i <= n; i++) {
            vm.roll(i * 2); // 每个检查点间隔 2 个区块
            cp.setValue(i);
            naive.setValue(i);
        }
        assertEq(cp.length(), n);

        // 查询最老的检查点（线性扫描最坏情况）
        vm.roll(n * 2 + 1);
        uint256 gBefore;

        gBefore = gasleft();
        uint256 bv = cp.valueAt(2);
        uint256 gasBinary = gBefore - gasleft();

        gBefore = gasleft();
        uint256 nv = naive.valueAt(2);
        uint256 gasLinear = gBefore - gasleft();

        assertEq(bv, 1);
        assertEq(nv, 1);

        emit log_named_uint("checkpoints", n);
        emit log_named_uint("gas binary (oldest target)", gasBinary);
        emit log_named_uint("gas linear (oldest target)", gasLinear);

        // 二分在 256 个检查点时必须显著便宜（冷存储访问除外的实测数量级）
        assertLt(gasBinary, gasLinear, "binary search should be cheaper at scale");
        // 二分查询应该是对数级：256 个检查点也只在很小的固定预算附近
        // （首次冷读检查点槽 2100 gas + 约 9 次迭代 × 100 gas warm）
        assertLt(gasBinary, 40_000, "binary query should stay near log-scale budget");
    }

    /// @notice 展示线性扫描 gas 随长度增长：小历史与大历史对比。
    function test_Gas_LinearGrowsWithHistory() public {
        // 小历史：16 个检查点
        for (uint256 i = 1; i <= 16; i++) {
            vm.roll(i * 2);
            naive.setValue(i);
        }
        vm.roll(100);
        uint256 beforeSmall = gasleft();
        naive.valueAt(2);
        uint256 gasSmall = beforeSmall - gasleft();

        // 全新大历史：256 个检查点（在另一个合约实例，避免存储预热干扰）
        NaiveCheckpoints big = new NaiveCheckpoints();
        for (uint256 i = 1; i <= 256; i++) {
            vm.roll(10_000 + i * 2);
            big.setValue(i);
        }
        vm.roll(20_000);
        uint256 beforeBig = gasleft();
        big.valueAt(10_002);
        uint256 gasBig = beforeBig - gasleft();

        emit log_named_uint("gas linear n=16  (oldest)", gasSmall);
        emit log_named_uint("gas linear n=256 (oldest)", gasBig);
        // 16 倍历史长度，线性扫描 gas 明显增长
        assertGt(gasBig, gasSmall * 4, "linear scan gas should grow with history length");
    }

    /// @notice 二分查找在小历史与大历史下都应保持平稳（对数级）。
    function test_Gas_BinaryStaysFlat() public {
        Checkpoints small = new Checkpoints();
        for (uint256 i = 1; i <= 16; i++) {
            vm.roll(i * 2);
            small.setValue(i);
        }
        vm.roll(100);
        uint256 beforeSmall = gasleft();
        small.valueAt(2);
        uint256 gasSmall = beforeSmall - gasleft();

        Checkpoints big = new Checkpoints();
        for (uint256 i = 1; i <= 256; i++) {
            vm.roll(10_000 + i * 2);
            big.setValue(i);
        }
        vm.roll(20_000);
        uint256 beforeBig = gasleft();
        big.valueAt(10_002);
        uint256 gasBig = beforeBig - gasleft();

        emit log_named_uint("gas binary n=16  (oldest)", gasSmall);
        emit log_named_uint("gas binary n=256 (oldest)", gasBig);
        // 从 16 到 256（16 倍），二分 gas 仅小幅增长：多 4 轮 warm 迭代
        // 加上若干冷存储槽（首次 2100/槽）。给 15_000 的宽松上限；
        // 对照线性扫描同区间增长远超此值（见 test_Gas_LinearGrowsWithHistory）。
        assertLt(gasBig - gasSmall, 15_000, "binary gas should grow only logarithmically");
    }
}

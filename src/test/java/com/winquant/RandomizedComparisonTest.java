package com.winquant;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 核心验收测试：随机事件流上，逐步将增量滑动窗口实现与
 * “取当前窗口全部值 + 全排序”参考实现逐点比较。
 * 覆盖负值、全重复、同时间戳、乱序、未来事件与空窗口。
 */
class RandomizedComparisonTest {

    private static final double[] QS = {0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0};

    @Test
    void compareAgainstFullSortOverRandomStreams() {
        long seed = 0xC0FFEEL;
        for (int scenario = 0; scenario < 6; scenario++) {
            runScenario(seed + scenario, scenario);
        }
    }

    private void runScenario(long seed, int scenario) {
        Random rnd = new Random(seed);
        long window = 20;
        ManualClock clock = new ManualClock(1000);
        SlidingWindowQuantile w = new SlidingWindowQuantile(window, clock);
        List<long[]> accepted = new ArrayList<>(); // 每项 {ts, value}
        long totalLate = 0;

        for (int step = 0; step < 4000; step++) {
            double r = rnd.nextDouble();
            if (r < 0.75) {
                // 时间戳围绕当前 now：含乱序（略早于 now 但在窗口内）与少量未来事件
                long ts = clock.now() + (long) (rnd.nextGaussian() * 6);
                long value;
                switch (scenario) {
                    case 0: // 全重复
                        value = 42;
                        break;
                    case 1: // 仅负值
                        value = -1 - value(rnd, 0, 49); // -1 .. -50
                        break;
                    case 2: // 小字母表，重复密集
                        value = value(rnd, 0, 5);
                        break;
                    case 3: // 大跨度含负数
                        value = (long) rnd.nextGaussian() * 100_000;
                        break;
                    case 4: // 同时间戳突发
                        ts = clock.now();
                        value = value(rnd, 0, 100);
                        break;
                    default: // 一般混合
                        value = (long) rnd.nextGaussian() * 100;
                }
                boolean ok = w.add(ts, value);
                if (ok) {
                    accepted.add(new long[]{ts, value});
                } else {
                    totalLate++;
                }
            } else {
                // 推进时间，步长含 0（同时间戳场景）和 1（逐格过期）
                clock.advanceBy(rnd.nextInt(6));
            }

            // 每一步都与参考实现比较
            final int stepNum = step;
            long now = clock.now();
            long cutoff = now - window;
            List<Long> expected = new ArrayList<>();
            for (long[] ev : accepted) {
                if (ev[0] > cutoff && ev[0] <= now) {
                    expected.add(ev[1]);
                }
            }
            long[] sorted = expected.stream().mapToLong(Long::longValue).sorted().toArray();

            assertEquals(sorted.length, w.count(),
                    () -> "scenario " + scenario + " step " + stepNum + ": count mismatch");
            assertEquals(totalLate, w.lateEventCount(),
                    () -> "scenario " + scenario + " step " + stepNum + ": late count mismatch");

            for (double q : QS) {
                if (sorted.length == 0) {
                    assertTrue(w.quantile(q).isEmpty(),
                            () -> "scenario " + scenario + " step " + stepNum + ": expected empty at q=" + q);
                } else {
                    double ref = Quantiles.quantileOfSorted(sorted, q);
                    double got = w.quantile(q).orElseThrow(
                            () -> new AssertionError("missing quantile step " + stepNum));
                    // 用 ulp 容忍 double 求值顺序差异
                    assertEquals(ref, got, Math.max(1e-9, Math.abs(ref) * 1e-12),
                            () -> "scenario " + scenario + " step " + stepNum
                                    + " q=" + q + " window=" + sorted.length);
                }
            }
        }
    }

    private static long value(Random rnd, int lo, int hi) {
        return lo + rnd.nextInt(hi - lo + 1);
    }
}

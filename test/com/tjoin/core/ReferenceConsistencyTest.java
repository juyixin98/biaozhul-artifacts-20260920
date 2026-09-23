package com.tjoin.core;

import static com.tjoin.Asserts.assertEquals;
import static com.tjoin.Asserts.assertTrue;

import com.tjoin.Test;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

/**
 * 流式算子 vs 小数据精确参考实现的差分（对拍）测试。
 *
 * <p>随机生成小规模事件流（含多 key、乱序、值重复、ID 重放、
 * 两侧独立水位线推进及一侧长时间停滞），以相同驱动顺序喂给两边，
 * 断言：输出配对集合完全一致，且每个配对恰好一次。
 */
public class ReferenceConsistencyTest {

    private static final class Op {
        final StreamSide side;
        final long wm;

        Op(StreamSide side, long wm) {
            this.side = side;
            this.wm = wm;
        }
    }

    private static final class Driver {
        final IntervalJoinOperator op;
        final ReferenceIntervalJoin ref;
        final Set<JoinPair> opPairs = new HashSet<>();
        final Set<JoinPair> refPairs = new HashSet<>();

        Driver(JoinConfig cfg) {
            this.op = new IntervalJoinOperator(cfg);
            this.ref = new ReferenceIntervalJoin(cfg);
        }

        void event(Event e) {
            List<JoinPair> a = op.processEvent(e, null);
            List<JoinPair> b = ref.processEvent(e);
            // 同一事件触发的一批输出：集合必须一致（桶内遍历顺序两边实现可能不同）
            assertEquals(new HashSet<>(b), new HashSet<>(a), "per-event outputs differ for " + e);
            assertEquals(b.size(), a.size(), "per-event output count differs for " + e);
            for (JoinPair p : a) {
                assertTrue(opPairs.add(p), "operator emitted duplicate pair " + p);
            }
            for (JoinPair p : b) {
                assertTrue(refPairs.add(p), "reference emitted duplicate pair " + p);
            }
        }

        void watermark(StreamSide side, long wm) {
            op.processWatermark(side, wm);
            ref.processWatermark(side, wm);
        }

        void assertSame() {
            assertEquals(refPairs.size(), opPairs.size(), "pair count mismatch");
            assertEquals(refPairs, opPairs, "pair sets differ");
        }

        static String describe(List<JoinPair> ps) {
            List<String> s = new ArrayList<>();
            for (JoinPair p : ps) {
                s.add(p.left().id() + ":" + p.right().id());
            }
            return String.join(",", s);
        }
    }

    @Test
    public void randomStreamsMatchReference() {
        long seed = 20260923L;
        Random rnd = new Random(seed);
        for (int iter = 0; iter < 300; iter++) {
            long lower = -rnd.nextLong(5);      // [-4, 0]
            long upper = rnd.nextLong(6);        // [0, 5]
            JoinConfig cfg = JoinConfig.builder().lowerBound(lower).upperBound(upper).build();
            runOneRandom(rnd, cfg, false);
        }
    }

    @Test
    public void randomStreamsWithExclusiveBoundsMatchReference() {
        Random rnd = new Random(424242L);
        for (int iter = 0; iter < 200; iter++) {
            JoinConfig cfg = JoinConfig.builder()
                    .lowerBound(-rnd.nextLong(4))
                    .upperBound(rnd.nextLong(5))
                    .lowerInclusive(rnd.nextBoolean())
                    .upperInclusive(rnd.nextBoolean())
                    .build();
            runOneRandom(rnd, cfg, false);
        }
    }

    @Test
    public void randomStreamsWithDuplicatesAndStallMatchReference() {
        Random rnd = new Random(7L);
        for (int iter = 0; iter < 200; iter++) {
            JoinConfig cfg = JoinConfig.symmetric(rnd.nextLong(4) + 1);
            runOneRandom(rnd, cfg, true);
        }
    }

    private void runOneRandom(Random rnd, JoinConfig cfg, boolean allowStallAndDup) {
        Driver d = new Driver(cfg);
        int keys = 1 + rnd.nextInt(3);
        int n = 6 + rnd.nextInt(10);

        long[] leftMax = {Long.MIN_VALUE};
        long[] rightMax = {Long.MIN_VALUE};
        long[] leftWm = {Long.MIN_VALUE};
        long[] rightWm = {Long.MIN_VALUE};

        // 每 (key, side) 的已发事件池，用于真实重放（相同 ID + 相同时间戳）
        List<Event>[][] pool = new List[keys][2];
        for (int k = 0; k < keys; k++) {
            pool[k][0] = new ArrayList<>();
            pool[k][1] = new ArrayList<>();
        }
        int seq = 0;
        for (int i = 0; i < n; i++) {
            // 以一定概率推进某侧水位线；stall 模式下右侧有概率长时间完全不推进
            if (rnd.nextInt(100) < 35) {
                StreamSide side = rnd.nextBoolean() ? StreamSide.LEFT : StreamSide.RIGHT;
                if (allowStallAndDup && side == StreamSide.RIGHT && rnd.nextInt(100) < 60) {
                    // 右流停滞：跳过水位线推进
                    continue;
                }
                long bump = 1 + rnd.nextLong(4);
                if (side == StreamSide.LEFT) {
                    leftWm[0] = Math.max(leftWm[0], leftMax[0] == Long.MIN_VALUE ? bump : leftMax[0] + bump - 3);
                    d.watermark(side, leftWm[0]);
                } else {
                    rightWm[0] = Math.max(rightWm[0], rightMax[0] == Long.MIN_VALUE ? bump : rightMax[0] + bump - 3);
                    d.watermark(side, rightWm[0]);
                }
                continue;
            }

            StreamSide side = rnd.nextBoolean() ? StreamSide.LEFT : StreamSide.RIGHT;
            int ki = rnd.nextInt(keys);
            String key = "k" + ki;
            int sideIdx = side == StreamSide.LEFT ? 0 : 1;

            // 真实重放：从该 (key, side) 池中取一个完全相同的事件
            boolean replay = allowStallAndDup
                    && !pool[ki][sideIdx].isEmpty()
                    && rnd.nextInt(100) < 20;
            Event ev;
            if (replay) {
                ev = pool[ki][sideIdx].get(rnd.nextInt(pool[ki][sideIdx].size()));
            } else {
                long ts = rnd.nextLong(20); // 0..19，制造乱序
                String id = key + "-" + side.name().charAt(0) + "-" + (seq++);
                Object value = rnd.nextBoolean() ? "v" : rnd.nextInt(3); // 允许值重复
                ev = new Event(id, side, key, ts, value);
                pool[ki][sideIdx].add(ev);
            }
            d.event(ev);
            if (side == StreamSide.LEFT) {
                leftMax[0] = Math.max(leftMax[0], ev.timestamp());
            } else {
                rightMax[0] = Math.max(rightMax[0], ev.timestamp());
            }
        }

        // 流末两侧各自推进到大水位线，所有应过期的状态被清理
        d.watermark(StreamSide.LEFT, 1_000_000L);
        d.watermark(StreamSide.RIGHT, 1_000_000L);
        d.assertSame();
        assertEquals(0, d.op.bufferedCount(StreamSide.LEFT), "all left expired at end");
        assertEquals(0, d.op.bufferedCount(StreamSide.RIGHT), "all right expired at end");
    }

    @Test
    public void exhaustiveSmallTimeline() {
        // 穷举：两侧各 3 个事件，时间戳取自 {0,1,2}，区间 [-1,+1]，所有交错方式太多，
        // 这里固定先按时间排序喂入，枚举每个事件左右身份与 ID 组合的一个代表集。
        JoinConfig cfg = JoinConfig.builder().lowerBound(-1L).upperBound(1L).build();
        Random rnd = new Random(99L);
        for (int iter = 0; iter < 500; iter++) {
            Driver d = new Driver(cfg);
            List<Event> all = new ArrayList<>();
            int idl = 0, idr = 0;
            for (long t = 0; t < 3; t++) {
                if (rnd.nextBoolean()) {
                    all.add(new Event("L" + idl++, StreamSide.LEFT, "k", t, "v"));
                }
                if (rnd.nextBoolean()) {
                    all.add(new Event("R" + idr++, StreamSide.RIGHT, "k", t, "v"));
                }
            }
            // 随机打乱到达顺序（保留时间戳）
            java.util.Collections.shuffle(all, rnd);
            for (Event e : all) {
                d.event(e);
            }
            d.watermark(StreamSide.LEFT, 100L);
            d.watermark(StreamSide.RIGHT, 100L);
            d.assertSame();
        }
    }
}

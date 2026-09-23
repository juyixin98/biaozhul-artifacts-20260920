package ij;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.TreeSet;

/**
 * 验收测试：
 * 1) 固定场景 —— 乱序到达、两侧水位推进不均、闭区间时间边界；
 * 2) 随机全量对比 —— 流式结果必须与 {@link OfflineJoin} 离线全量连接一致；
 * 3) 水位不均 —— 单侧水位先到不得提前回收；
 * 4) 极端热键 —— 单 key 大量事件的性能与回收量。
 */
final class AcceptanceTest {

    private final Assert a;

    AcceptanceTest(Assert a) {
        this.a = a;
        deterministicScenario();
        randomizedVsOffline();
        unevenWatermarks();
        hotKey();
    }

    // ---------------- 1) 固定场景：乱序 + 时间边界 ----------------

    private void deterministicScenario() {
        final long lower = -2, upper = 2;
        // key=u 的两侧事件（含边界 3/7，含同时间 5=5，含不相关 key）。
        long[] leftTs = {5, 10, 5};          // 两个 L@5（不同 id），一个 L@10
        long[] rightTs = {3, 4, 5, 6, 7, 8, 100};

        List<OfflineJoin.RefEvent> offlineL = new ArrayList<>();
        List<OfflineJoin.RefEvent> offlineR = new ArrayList<>();

        JoinSession s = new JoinSession("acc", lower, upper);
        // 故意打乱到达顺序（右先于左、时间逆序）。
        List<Runnable> feed = new ArrayList<>();
        int li = 0;
        for (long ts : leftTs) {
            String id = "L" + li++;
            offlineL.add(new OfflineJoin.RefEvent(id, "u", ts));
            feed.add(() -> s.pushEvent("left", "u", ts, id, null));
        }
        int ri = 0;
        for (long ts : rightTs) {
            String id = "R" + ri++;
            offlineR.add(new OfflineJoin.RefEvent(id, "u", ts));
            feed.add(() -> s.pushEvent("right", "u", ts, id, null));
        }
        // 不同 key，永不配对。
        feed.add(() -> s.pushEvent("left", "other", 5L, "X1", null));
        feed.add(() -> s.pushEvent("right", "other2", 5L, "X2", null));

        // 先用洗牌（固定种子），但保证水位在所有事件之后才推进：无迟到。
        Collections.shuffle(feed, new Random(42));
        for (Runnable r : feed) {
            r.run();
        }

        TreeSet<String> expected = OfflineJoin.fullOuterPairs(offlineL, offlineR, lower, upper);
        a.eq(joinSigs(s), expected, "deterministic shuffled scenario matches offline full join");

        // L@5 x2 都应与 R@3..7 配对（2 x5 = 10）；L@10 与 R@8 配对（100 不命中）。
        a.eq(s.pairs().size(), 11L, "pair count incl. duplicate timestamps");

        // 两侧水位不均：左先到 100，右侧不动 -> 一个都不能回收。
        s.advanceWatermark("left", 1000);
        a.eq(stat(s, "totalReclaimed"), 0L, "no reclaim while right watermark at -inf");
        a.eq(stat(s, "leftStateEvents"), 4L, "left events retained despite own watermark ahead");

        // 右水位追上后全部回收（u 左3右7 + 两个不相关 key 事件 = 12）。
        s.advanceWatermark("right", 1000);
        a.eq(stat(s, "leftStateEvents"), 0L, "all left reclaimed after both watermarks");
        a.eq(stat(s, "rightStateEvents"), 0L, "all right reclaimed after both watermarks");
        a.eq(stat(s, "totalReclaimed"), 12L, "reclaimed count = 4 left + 8 right");
        a.eq(s.pairs().size(), 11L, "pairs still queryable after reclaim");
    }

    // ---------------- 2) 随机 fuzz：对比离线全量连接 ----------------

    private void randomizedVsOffline() {
        Random rnd = new Random(20260923L);
        int runs = 0;
        for (int trial = 0; trial < 40; trial++) {
            long lower = rnd.nextInt(5) - 2;            // -2..2
            long upper = lower + rnd.nextInt(4);        // lower..lower+3
            int keyCount = 1 + rnd.nextInt(4);
            JoinSession s = new JoinSession("fuzz" + trial, lower, upper);

            List<OfflineJoin.RefEvent> offlineL = new ArrayList<>();
            List<OfflineJoin.RefEvent> offlineR = new ArrayList<>();
            List<Runnable> feed = new ArrayList<>();

            for (int k = 0; k < keyCount; k++) {
                String key = "k" + k;
                int nL = 1 + rnd.nextInt(12);
                int nR = 1 + rnd.nextInt(12);
                for (int i = 0; i < nL; i++) {
                    long ts = rnd.nextInt(40);
                    String id = trial + "-L" + k + "-" + i + "@" + ts;
                    offlineL.add(new OfflineJoin.RefEvent(id, key, ts));
                    feed.add(() -> s.pushEvent("left", key, ts, id, null));
                }
                for (int i = 0; i < nR; i++) {
                    long ts = rnd.nextInt(40);
                    String id = trial + "-R" + k + "-" + i + "@" + ts;
                    offlineR.add(new OfflineJoin.RefEvent(id, key, ts));
                    feed.add(() -> s.pushEvent("right", key, ts, id, null));
                }
            }

            // 中间穿插推进水位，但保持在 -100，绝不丢弃事件；也不应触发回收（阈值极低）。
            for (int i = 0; i < 5; i++) {
                int idx = rnd.nextInt(feed.size());
                feed.add(idx, () -> s.advanceWatermark(rnd.nextBoolean() ? "left" : "right", -100));
            }

            Collections.shuffle(feed, rnd);
            for (Runnable r : feed) {
                r.run();
            }

            TreeSet<String> expected = OfflineJoin.fullOuterPairs(offlineL, offlineR, lower, upper);
            boolean ok = joinSigs(s).equals(expected);
            a.check(ok, "fuzz trial " + trial + " (bounds [" + lower + "," + upper + "], keys="
                    + keyCount + ", pairs=" + expected.size() + ") matches offline");
            runs++;
        }
        a.check(runs == 40, "40 fuzz trials executed");
    }

    // ---------------- 3) 两侧水位推进不均的确定性检查 ----------------

    private void unevenWatermarks() {
        JoinSession s = new JoinSession("uneven", -2, 2);
        // key a：早事件；key b：晚事件（验证回收只按时间，不把热键之外的状态误删）。
        s.pushEvent("left", "a", 1L, "La1", null);
        s.pushEvent("right", "a", 1L, "Ra1", null);
        s.pushEvent("left", "b", 50L, "Lb1", null);
        s.pushEvent("right", "b", 50L, "Rb1", null);

        // 阶段 1：只有右水位猛进到 60。
        // cutR = min(60, MIN-(-2)=MIN) = MIN：右事件全部保留；左侧阈值同理保留。
        s.advanceWatermark("right", 60);
        a.eq(stat(s, "leftStateEvents"), 2L, "uneven: left all retained (wl=-inf)");
        a.eq(stat(s, "rightStateEvents"), 2L, "uneven: right all retained (wl=-inf)");

        // 阶段 2：左水位到 3（右水位仍 60）。
        // cutL = min(3, 60-2=58) = 3：L a@1 回收，L b@50 保留。
        // cutR = min(60, 3+2=5) = 5：R a@1 回收，R b@50 保留。
        s.advanceWatermark("left", 3);
        a.eq(stat(s, "leftReclaimed"), 1L, "uneven: only old left event reclaimed");
        a.eq(stat(s, "rightReclaimed"), 1L, "uneven: only old right event reclaimed");
        a.eq(stat(s, "leftStateEvents"), 1L, "uneven: late-key left event retained");
        a.eq(stat(s, "rightStateEvents"), 1L, "uneven: late-key right event retained");

        // 阶段 3：左水位补到 60，剩余 b@50 全部回收。
        s.advanceWatermark("left", 60);
        a.eq(stat(s, "totalReclaimed"), 4L, "uneven: final total reclaimed 4");

        // 水位不回退：重复/更小的推进不产生回收也不改变状态。
        int delta = s.advanceWatermark("left", 40);
        a.eq(delta, 0L, "watermark does not regress");
        a.eq(stat(s, "totalReclaimed"), 4L, "regressing call reclaims nothing");
    }

    // ---------------- 4) 极端热键 ----------------

    private void hotKey() {
        final int n = 800;
        JoinSession s = new JoinSession("hot", 0, 0); // 仅同时间配对

        List<OfflineJoin.RefEvent> offlineL = new ArrayList<>();
        List<OfflineJoin.RefEvent> offlineR = new ArrayList<>();
        // 左：0..n-1 各一个；右：每个时间点两个事件（制造扇出）。
        for (int i = 0; i < n; i++) {
            s.pushEvent("left", "HOT", (long) i, "L" + i, null);
            offlineL.add(new OfflineJoin.RefEvent("L" + i, "HOT", i));
        }
        // 逆序注入右事件，进一步考验有序索引。
        for (int i = n - 1; i >= 0; i--) {
            s.pushEvent("right", "HOT", (long) i, "R" + i + "a", null);
            s.pushEvent("right", "HOT", (long) i, "R" + i + "b", null);
            offlineR.add(new OfflineJoin.RefEvent("R" + i + "a", "HOT", i));
            offlineR.add(new OfflineJoin.RefEvent("R" + i + "b", "HOT", i));
        }

        long start = System.nanoTime();
        TreeSet<String> expected = OfflineJoin.fullOuterPairs(offlineL, offlineR, 0, 0);
        long offlineMs = (System.nanoTime() - start) / 1_000_000;

        a.eq((long) s.pairs().size(), (long) expected.size(),
                "hot key pair count equals offline (" + expected.size() + " pairs)");
        a.eq(joinSigs(s), expected, "hot key pairs equal offline set");
        a.eq(stat(s, "leftStateEvents"), (long) n, "hot key left state count");
        a.eq(stat(s, "rightStateEvents"), (long) 2 * n, "hot key right state count");

        // 单侧水位提前，不得回收任何事件。
        s.advanceWatermark("left", n * 2L);
        a.eq(stat(s, "totalReclaimed"), 0L, "hot key: no reclaim with one watermark");

        // 双侧到齐后一次性回收 3n 个事件，验证回收状态量输出。
        s.advanceWatermark("right", n * 2L);
        a.eq(stat(s, "totalReclaimed"), (long) 3 * n, "hot key: all 3n events reclaimed");
        a.eq(stat(s, "leftStateEvents"), 0L, "hot key: state drained");
        a.eq(stat(s, "leftStateKeys"), 0L, "hot key: empty key removed");

        System.out.println("    [info] hot key: n=" + n + ", pairs=" + expected.size()
                + ", offline cross-check=" + offlineMs + "ms");
    }

    // ---------------- 辅助 ----------------

    private TreeSet<String> joinSigs(JoinSession s) {
        TreeSet<String> out = new TreeSet<>();
        for (Pair p : s.pairs()) {
            out.add(p.left.key + ":" + p.left.id + "@" + p.left.ts + "-" + p.right.id + "@" + p.right.ts);
        }
        return out;
    }

    private long stat(JoinSession s, String k) {
        return ((Number) s.snapshotStats().get(k)).longValue();
    }
}

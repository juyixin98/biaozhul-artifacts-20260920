package topk;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.TreeMap;

/**
 * 核心语义测试。Oracle 用“保存活跃事件再重算”的方式实现——
 * 这只允许出现在测试里，用来逐事件对照被测服务的增量结果。
 */
public final class TopKServiceTest {

    /** 重算式参照实现：与 TopKService 语义保持一致。 */
    static final class Oracle {
        record Ev(String group, String key, long delta, long ts) {}

        final long windowMs;
        long watermark = Long.MIN_VALUE;
        final Map<String, Ev> active = new LinkedHashMap<>();
        final Set<String> seen = new HashSet<>();

        Oracle(long windowMs) { this.windowMs = windowMs; }

        boolean expired(long ts) {
            return watermark != Long.MIN_VALUE && ts <= watermark - windowMs;
        }

        void advanceTo(long now) { if (now > watermark) watermark = now; }

        String insert(String id, String group, String key, long delta, long ts) {
            if (!seen.add(id)) return "DUPLICATE_EVENT_ID";
            advanceTo(ts);
            if (expired(ts)) return "ALREADY_EXPIRED";
            active.put(id, new Ev(group, key, delta, ts));
            return "APPLIED";
        }

        boolean retract(String id) {
            Ev e = active.get(id);
            if (e == null) return false;
            expire();
            return active.remove(id) != null;
        }

        void expire() {
            if (watermark == Long.MIN_VALUE) return;
            active.values().removeIf(e -> expired(e.ts()));
        }

        List<TopKService.Entry> ranking(String group, long now) {
            advanceTo(now);
            expire();
            Map<String, Long> scores = new HashMap<>();
            for (Ev e : active.values()) {
                if (!e.group().equals(group)) continue;
                long s = scores.getOrDefault(e.key(), 0L) + e.delta();
                if (s == 0) scores.remove(e.key());
                else scores.put(e.key(), s);
            }
            List<TopKService.Entry> out = new ArrayList<>();
            scores.forEach((k, v) -> out.add(new TopKService.Entry(v, k)));
            out.sort(null);
            return out;
        }
    }

    // ---------- 验收场景 1：并列名次，分数相同按 key 升序 ----------
    static void tiedRanksBrokenByKey() {
        TopKService s = new TopKService(10_000);
        s.insert("e1", "g", "banana", 5, 1000);
        s.insert("e2", "g", "apple", 5, 1000);
        s.insert("e3", "g", "cherry", 5, 1000);
        s.insert("e4", "g", "date", 7, 1000);
        List<TopKService.Entry> r = s.ranking("g", 1000);
        assertKeys(r, "date", "apple", "banana", "cherry");
        // topK 截断后并列顺序不变
        assertKeys(s.topK("g", 3, 1000), "date", "apple", "banana");
    }

    // ---------- 验收场景 2：负增量 ----------
    static void negativeDeltas() {
        TopKService s = new TopKService(10_000);
        s.insert("e1", "g", "a", 10, 1000);
        s.insert("e2", "g", "a", -4, 1001);   // a = 6
        s.insert("e3", "g", "b", -3, 1002);   // b = -3（负分也参与排名）
        s.insert("e4", "g", "c", 6, 1003);    // c = 6，与 a 并列，按 key 升序
        List<TopKService.Entry> r = s.ranking("g", 2000);
        assertKeys(r, "a", "c", "b");
        assertScore(r, "a", 6);
        assertScore(r, "c", 6);
        assertScore(r, "b", -3);
        // 负增量把分数打到 0：元素移出排名
        s.insert("e5", "g", "c", -6, 1004);
        assertKeys(s.ranking("g", 2000), "a", "b");
    }

    // ---------- 验收场景 3：K 大于元素数 ----------
    static void kLargerThanElementCount() {
        TopKService s = new TopKService(10_000);
        s.insert("e1", "g", "a", 3, 1000);
        s.insert("e2", "g", "b", 1, 1000);
        List<TopKService.Entry> r = s.topK("g", 10, 1000);
        Assert.eq(2, r.size(), "k > n 时返回全部元素");
        assertKeys(r, "a", "b");
        // 空分组
        Assert.eq(0, s.topK("nope", 5, 1000).size(), "空分组返回空列表");
    }

    // ---------- 验收场景 4：窗口滑出，过期只扣减一次 ----------
    static void windowSlideOut() {
        TopKService s = new TopKService(1000);
        s.insert("e1", "g", "a", 5, 1000);
        s.insert("e2", "g", "a", 3, 1500);
        s.insert("e3", "g", "b", 4, 1900);
        // now=2000：窗口 (1000, 2000]，e1(ts=1000) 滑出，a = 3
        List<TopKService.Entry> r = s.ranking("g", 2000);
        assertKeys(r, "b", "a");
        assertScore(r, "a", 3);
        // 再次查询：e1 不得被重复扣减
        r = s.ranking("g", 2001);
        assertScore(r, "a", 3);
        // now=2500：e2(ts=1500) 滑出，a 净分 0 移出
        r = s.ranking("g", 2500);
        assertKeys(r, "b");
        // 过期后再撤回 e1：空操作，不得再次扣减
        Assert.eq(false, s.retract("e1"), "过期后撤回应为空操作");
        Assert.eq(false, s.retract("e2"), "过期后撤回应为空操作");
        assertKeys(s.ranking("g", 2500), "b");
        assertScore(s.ranking("g", 2500), "b", 4);
    }

    // ---------- 撤回只扣减一次（先撤回后过期） ----------
    static void retractThenExpireDeductsOnce() {
        TopKService s = new TopKService(1000);
        s.insert("e1", "g", "a", 5, 1000);
        s.insert("e2", "g", "a", 2, 1100);
        Assert.eq(true, s.retract("e1"), "撤回活跃事件应成功");
        assertScore(s.ranking("g", 1100), "a", 2);
        // 时间推进到 now=2000：e1(ts=1000) 若未撤回本会在此过期，不得二次扣减；e2(ts=1100) 仍在窗口内
        List<TopKService.Entry> r = s.ranking("g", 2000);
        assertScore(r, "a", 2);
        // 重复撤回：幂等空操作
        Assert.eq(false, s.retract("e1"), "重复撤回应为空操作");
        Assert.eq(false, s.retract("never-existed"), "撤回未知 id 应为空操作");
        assertScore(s.ranking("g", 2000), "a", 2);
    }

    // ---------- 撤回与并列组合：撤回后并列顺序恢复 ----------
    static void retractRestoresTieOrder() {
        TopKService s = new TopKService(10_000);
        s.insert("e1", "g", "a", 5, 1000);
        s.insert("e2", "g", "b", 5, 1000);
        s.insert("e3", "g", "b", 2, 1000); // b=7 领先
        assertKeys(s.ranking("g", 1000), "b", "a");
        s.retract("e3");                    // b 回到 5，与 a 并列，a 在前
        assertKeys(s.ranking("g", 1000), "a", "b");
    }

    // ---------- 重复 eventId 拒绝 ----------
    static void duplicateEventIdRejected() {
        TopKService s = new TopKService(10_000);
        Assert.eq(TopKService.InsertResult.APPLIED, s.insert("e1", "g", "a", 5, 1000), "首次插入");
        Assert.eq(TopKService.InsertResult.DUPLICATE_EVENT_ID, s.insert("e1", "g", "a", 99, 1000), "重复 id");
        assertScore(s.ranking("g", 1000), "a", 5);
        // 撤回后同 id 仍拒绝（事件 id 全局一次性）
        s.retract("e1");
        Assert.eq(TopKService.InsertResult.DUPLICATE_EVENT_ID, s.insert("e1", "g", "a", 1, 1000), "撤回后重复 id");
    }

    // ---------- 迟到事件：插入即过期，不计分 ----------
    static void lateEventDropped() {
        TopKService s = new TopKService(1000);
        s.insert("e1", "g", "a", 5, 5000);
        Assert.eq(TopKService.InsertResult.ALREADY_EXPIRED,
                s.insert("e2", "g", "b", 7, 1000), "ts 已滑出窗口的事件");
        assertKeys(s.ranking("g", 5000), "a");
    }

    // ---------- 分组隔离 ----------
    static void groupsAreIsolated() {
        TopKService s = new TopKService(1000);
        s.insert("e1", "g1", "a", 5, 1000);
        s.insert("e2", "g2", "a", 9, 1000);
        assertScore(s.ranking("g1", 1000), "a", 5);
        assertScore(s.ranking("g2", 1000), "a", 9);
    }

    // ---------- 逐事件对照：随机操作序列 vs 重算式 Oracle ----------
    static void randomizedReplayAgainstOracle() {
        long[] seeds = {1, 42, 20260923};
        for (long seed : seeds) {
            Random rnd = new Random(seed);
            TopKService svc = new TopKService(500);
            Oracle oracle = new Oracle(500);
            String[] groups = {"g1", "g2", "g3"};
            String[] keys = {"a", "b", "c", "d", "e"};
            List<String> insertedIds = new ArrayList<>();
            long now = 0;
            int eventSeq = 0;

            for (int step = 0; step < 3000; step++) {
                int op = rnd.nextInt(100);
                if (op < 55) { // 插入
                    String id = "ev" + (eventSeq++);
                    String g = groups[rnd.nextInt(groups.length)];
                    String k = keys[rnd.nextInt(keys.length)];
                    long delta = rnd.nextInt(21) - 10; // -10..10，含负增量
                    long ts = now + rnd.nextInt(50);   // 允许小幅乱序/迟到
                    String expect = oracle.insert(id, g, k, delta, ts);
                    String actual = svc.insert(id, g, k, delta, ts).name();
                    Assert.eq(expect, actual, "insert result seed=" + seed + " step=" + step);
                    insertedIds.add(id);
                } else if (op < 75) { // 撤回（含不存在的 id）
                    String id = insertedIds.isEmpty() || rnd.nextInt(10) == 0
                            ? "ghost" + rnd.nextInt(5)
                            : insertedIds.get(rnd.nextInt(insertedIds.size()));
                    Assert.eq(oracle.retract(id), svc.retract(id), "retract result seed=" + seed + " step=" + step);
                } else { // 推进时间
                    now += 1 + rnd.nextInt(200);
                }
                // 每步之后：逐事件对照所有分组的窗口内完整排序
                for (String g : groups) {
                    List<TopKService.Entry> expect = oracle.ranking(g, now);
                    List<TopKService.Entry> actual = svc.ranking(g, now);
                    Assert.eq(expect, actual,
                            "ranking mismatch seed=" + seed + " step=" + step + " group=" + g);
                    // topK 必须是完整排序的前 k 名
                    int k = 1 + rnd.nextInt(8); // 覆盖 k > 元素数
                    List<TopKService.Entry> top = svc.topK(g, k, now);
                    List<TopKService.Entry> prefix = expect.subList(0, Math.min(k, expect.size()));
                    Assert.eq(prefix, top, "topK mismatch seed=" + seed + " step=" + step + " k=" + k);
                }
            }
        }
    }

    // ---------- 小工具 ----------
    static void assertKeys(List<TopKService.Entry> ranking, String... keys) {
        List<String> actual = new ArrayList<>();
        for (TopKService.Entry e : ranking) actual.add(e.key());
        Assert.eq(List.of(keys), actual, "排名顺序");
    }

    static void assertScore(List<TopKService.Entry> ranking, String key, long score) {
        for (TopKService.Entry e : ranking) {
            if (e.key().equals(key)) {
                Assert.eq(score, e.score(), "score of " + key);
                return;
            }
        }
        throw new AssertionError("key " + key + " not in ranking " + ranking);
    }

    public static void runAll() {
        Map<String, Runnable> tests = new TreeMap<>();
        tests.put("tiedRanksBrokenByKey", TopKServiceTest::tiedRanksBrokenByKey);
        tests.put("negativeDeltas", TopKServiceTest::negativeDeltas);
        tests.put("kLargerThanElementCount", TopKServiceTest::kLargerThanElementCount);
        tests.put("windowSlideOut", TopKServiceTest::windowSlideOut);
        tests.put("retractThenExpireDeductsOnce", TopKServiceTest::retractThenExpireDeductsOnce);
        tests.put("retractRestoresTieOrder", TopKServiceTest::retractRestoresTieOrder);
        tests.put("duplicateEventIdRejected", TopKServiceTest::duplicateEventIdRejected);
        tests.put("lateEventDropped", TopKServiceTest::lateEventDropped);
        tests.put("groupsAreIsolated", TopKServiceTest::groupsAreIsolated);
        tests.put("randomizedReplayAgainstOracle", TopKServiceTest::randomizedReplayAgainstOracle);
        tests.forEach((name, t) -> {
            t.run();
            System.out.println("PASS " + name);
        });
    }
}

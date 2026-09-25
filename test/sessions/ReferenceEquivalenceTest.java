package sessions;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import sessions.agg.AggregateFunction;
import sessions.agg.CountAggregate;
import sessions.agg.SumAggregate;
import sessions.model.Event;
import sessions.model.WindowUpdate;
import sessions.op.SessionWindowOperator;
import sessions.reference.ChangelogFold;
import sessions.reference.ReferenceSessions;
import sessions.testing.Assert;
import sessions.testing.Test;
import sessions.time.SimTimerService;

/**
 * 验收核心：流式算子（喂入乱序事件 + 水位线）的最终结果，
 * 必须等于离线"完整分组"参考实现对<b>同一批被接受事件</b>的结果。
 *
 * <p>另外校验 changelog 的账本性质：每条 RETRACT 必须精确对应此前一条 ADD，
 * 以及 finish() 后状态全部清空。
 */
public final class ReferenceEquivalenceTest {

    @Test("手工剧本：乱序+多次迟到桥接，结果等于离线分组")
    static void fixedScenarioMatchesReference() {
        long gap = 10;
        long lateness = 20;
        // (事件时间, 喂入后推进到的水位线) 序列
        long[][] raw = {
                {1, 1}, {5, 1}, {21, 16}, {25, 16},
                {40, 35}, {45, 35}, // W=35：封存 [1,5](sealAt15/purgeAt35 仍保留) 与 [21,25](sealAt35)；[40,45] 活动
                {15, 35},   // 桥接两个已封存窗口（门控边界 W-L=15）
                {35, 44},   // 桥接 [1,25] 与活动窗 [40,45]（40-35=5<=gap，W-L=24<=35）
        };
        List<Event> fed = new ArrayList<>();
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op =
                new SessionWindowOperator(gap, lateness, new SumAggregate(), ts);
        for (long[] r : raw) {
            Event event = new Event("a", r[0], r[0]);
            op.processElement(event);
            fed.add(event);
            op.advanceWatermark(r[1]);
        }
        op.finish();

        List<ChangelogFold.Row> streamed = ChangelogFold.fold(op.changelog());
        List<ReferenceSessions.Result> reference =
                ReferenceSessions.groupAll(fed, gap, new SumAggregate());

        Assert.assertEquals(reference.size(), streamed.size(),
                "window count equals reference");
        for (int i = 0; i < reference.size(); i++) {
            ReferenceSessions.Result ref = reference.get(i);
            ChangelogFold.Row row = streamed.get(i);
            Assert.assertEquals(ref.key(), row.key(), "key");
            Assert.assertEquals(ref.window().start(), row.start(), "start");
            Assert.assertEquals(ref.window().end(), row.end(), "end");
            Assert.assertEquals(ref.aggregate(), row.aggregate(), "aggregate(sum=ts)");
        }
        Assert.assertEquals(0, op.droppedLateEvents(), "no drops with L=20");
        // 最终只有一个桥接后的会话 [1,45]，sum=1+5+21+25+40+45+15+35
        Assert.assertEquals(1, streamed.size(), "everything bridged into one session");
        Assert.assertEquals(187L, streamed.get(0).aggregate(), "merged sum");
    }

    @Test("账本性质：每条 RETRACT 精确对应一条尚未被撤回的 ADD")
    static void changelogIsACorrectLedger() {
        long gap = 10;
        long lateness = 20;
        long[][] raw = {
                {1, 1}, {21, 16}, {15, 36}, {5, 36}, {11, 50}, {60, 60},
        };
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op =
                new SessionWindowOperator(gap, lateness, new SumAggregate(), ts);
        for (long[] r : raw) {
            op.processElement(new Event("k", r[0], r[0]));
            op.advanceWatermark(r[1]);
        }
        // fold() 内部会校验 ADD 覆盖/RETRACT 不匹配并抛异常
        List<ChangelogFold.Row> rows = ChangelogFold.fold(op.changelog());
        Assert.assertTrue(!rows.isEmpty(), "some final results exist");
    }

    @Test("随机性质：大迟到容忍（无丢弃）下乱序流恒等于离线完整分组")
    static void randomizedNoDropsEquivalence() {
        Random rnd = new Random(20260923L);
        AggregateFunction<?> agg = new SumAggregate();
        for (int trial = 0; trial < 300; trial++) {
            long gap = 1 + rnd.nextInt(8);
            runOneRandomTrial(rnd, gap, 5000, agg, false);
        }
    }

    @Test("随机性质：有丢弃时被接受事件的 COUNT 与最终表一致且状态清空")
    static void randomizedWithDropsAcceptedCount() {
        Random rnd = new Random(42L);
        AggregateFunction<?> agg = new CountAggregate();
        for (int trial = 0; trial < 300; trial++) {
            long gap = 1 + rnd.nextInt(10);
            long lateness = rnd.nextInt(12);
            runOneRandomTrial(rnd, gap, lateness, agg, true);
        }
    }

    /**
     * @param allowDrops true 时允许事件被门控丢弃，校验"已接受事件计数 == 最终表计数"；
     *                   false 时要求零丢弃，并与离线完整分组逐窗精确对照。
     */
    private static void runOneRandomTrial(Random rnd, long gap, long lateness,
                                          AggregateFunction<?> agg, boolean allowDrops) {
        int keyCount = 1 + rnd.nextInt(3);
        List<Event> generated = new ArrayList<>();
        for (String key : keys(keyCount)) {
            long t = 0;
            int n = 20 + rnd.nextInt(30);
            for (int i = 0; i < n; i++) {
                t += rnd.nextInt(6); // 0..5 间隔，制造同会话/跨会话
                long value = agg instanceof SumAggregate ? rnd.nextInt(20) - 5 : 1;
                generated.add(new Event(key, t, value));
            }
        }
        // 全局洗牌：真正乱序到达（含跨 key 交错）
        List<Event> shuffled = new ArrayList<>(generated);
        java.util.Collections.shuffle(shuffled, rnd);

        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(gap, lateness, agg, ts);
        List<Event> accepted = new ArrayList<>();
        for (Event event : shuffled) {
            long before = op.receivedEvents() - op.droppedLateEvents();
            op.processElement(event);
            long after = op.receivedEvents() - op.droppedLateEvents();
            if (after > before) {
                accepted.add(event);
            }
            if (allowDrops) {
                op.advanceWatermark(Math.max(ts.currentWatermark(),
                        event.timestamp() - rnd.nextInt(15)));
            } else {
                op.advanceWatermark(event.timestamp());
            }
        }
        for (int i = 0; i < 5; i++) {
            op.advanceWatermark(rnd.nextInt(400));
        }
        op.finish();

        if (!allowDrops) {
            Assert.assertEquals(0, op.droppedLateEvents(), "trial must drop nothing");
            assertEqualsReference(generated, gap, agg, op.changelog());
        } else {
            long acceptedCount = op.receivedEvents() - op.droppedLateEvents();
            long counted = ChangelogFold.fold(op.changelog()).stream()
                    .mapToLong(ChangelogFold.Row::aggregate).sum();
            Assert.assertEquals(acceptedCount, counted,
                    "COUNT final table counts exactly the accepted events");
            // 被接受事件本身（乱序）的离线分组也必须与流式结果一致
            assertEqualsReference(accepted, gap, agg, op.changelog());
        }
        Assert.assertEquals(0, op.totalRetainedWindowCount(),
                "state fully cleaned after finish");
    }

    private static void assertEqualsReference(List<Event> events, long gap,
                                              AggregateFunction<?> agg,
                                              List<WindowUpdate> changelog) {
        List<ChangelogFold.Row> streamed = ChangelogFold.fold(changelog);
        List<ReferenceSessions.Result> reference =
                ReferenceSessions.groupAll(events, gap, agg);
        Assert.assertEquals(reference.size(), streamed.size(),
                "window count matches reference");

        Map<String, List<ChangelogFold.Row>> streamedByKey = new HashMap<>();
        for (ChangelogFold.Row r : streamed) {
            streamedByKey.computeIfAbsent(r.key(), k -> new ArrayList<>()).add(r);
        }
        for (ReferenceSessions.Result ref : reference) {
            List<ChangelogFold.Row> rows = streamedByKey.get(ref.key());
            ChangelogFold.Row match = rows == null ? null : rows.stream()
                    .filter(r -> r.start() == ref.window().start()
                            && r.end() == ref.window().end()
                            && r.aggregate() == ref.aggregate())
                    .findFirst().orElse(null);
            if (match == null) {
                Assert.fail("reference window missing from stream: key=" + ref.key()
                        + " window=" + ref.window() + " agg=" + ref.aggregate()
                        + " streamed=" + rows);
            }
            rows.remove(match);
        }
        for (List<ChangelogFold.Row> leftover : streamedByKey.values()) {
            Assert.assertTrue(leftover.isEmpty(),
                    "stream produced windows not in reference: " + leftover);
        }
    }

    private static List<String> keys(int n) {
        List<String> ks = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            ks.add("k" + i);
        }
        return ks;
    }
}

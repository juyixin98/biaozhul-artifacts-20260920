package sessions;

import java.util.List;

import sessions.agg.CountAggregate;
import sessions.agg.SumAggregate;
import sessions.model.Event;
import sessions.model.ResultKind;
import sessions.model.WindowUpdate;
import sessions.op.SessionWindowOperator;
import sessions.testing.Assert;
import sessions.testing.Test;
import sessions.time.SimTimerService;

/**
 * 会话窗口算子的核心语义测试，覆盖验收要求的三类场景：
 * 乱序桥接、边界间隔相等、封窗后迟到（含撤回/新增与状态清理）。
 */
public final class SessionWindowOperatorTest {

    // ---------------------------------------------------------------
    // 基础：准时事件合并、封窗、收尾
    // ---------------------------------------------------------------

    @Test("准时事件合并为一个会话并在 W>=end+gap 时封窗")
    static void basicMergeAndSeal() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new CountAggregate(), ts);
        op.processElement(e("a", 1));
        op.processElement(e("a", 5));
        op.processElement(e("a", 11)); // 间隔 4、6，均 <=10
        Assert.assertEquals(1, op.activeWindowCount("a"), "single active session");

        op.advanceWatermark(20); // end=11, sealAt=21；20<21 不封
        Assert.assertEquals(1, op.activeWindowCount("a"), "not sealed at W=20 (<21)");
        Assert.assertTrue(op.changelog().isEmpty(), "no output before seal");

        op.advanceWatermark(21); // W >= 21 封存（闭区间）
        Assert.assertEquals(0, op.activeWindowCount("a"), "sealed leaves active set");
        Assert.assertEquals(1, op.sealedWindowCount("a"), "still retained at W=purgeAt");
        op.advanceWatermark(22); // L=0，purgeAt=21，严格 W>21 才清除
        Assert.assertEquals(0, op.sealedWindowCount("a"), "L=0: purged once W>sealAt");
        WindowUpdate only = op.changelog().get(0);
        assertUpdate(only, ResultKind.ADD, "a", 1, 11, 3);
    }

    @Test("间隔大于 gap 的事件形成两个独立会话")
    static void separateSessions() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new SumAggregate(), ts);
        op.processElement(e("a", 1, 100));
        op.processElement(e("a", 22, 7)); // 间隔 21 > 10
        op.advanceWatermark(50);
        Assert.assertEquals(2, op.changelog().size(), "two sealed sessions");
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 1, 1, 100);
        assertUpdate(op.changelog().get(1), ResultKind.ADD, "a", 22, 22, 7);
    }

    @Test("finish() 封存全部活动窗口且清空全部状态")
    static void finishSealsEverything() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 5, new CountAggregate(), ts);
        op.processElement(e("a", 1));
        op.processElement(e("b", 2));
        op.finish();
        Assert.assertEquals(2, op.changelog().size(), "both keys emitted");
        Assert.assertEquals(0, op.totalRetainedWindowCount(), "no state retained after finish");
    }

    // ---------------------------------------------------------------
    // 验收：边界间隔相等（gap 是闭区间）
    // ---------------------------------------------------------------

    @Test("相邻事件间隔恰好等于 gap 时仍属同一会话")
    static void gapEqualityBoundaries() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new CountAggregate(), ts);
        op.processElement(e("a", 0));
        op.processElement(e("a", 10)); // 间隔 == gap：合并
        op.processElement(e("a", 20)); // 间隔 == gap：合并
        op.advanceWatermark(31); // end+gap=30，W=31 封窗
        Assert.assertEquals(1, op.changelog().size(), "one session across equal gaps");
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 0, 20, 3);
    }

    @Test("间隔恰好 gap+1 的事件分到两个会话")
    static void gapPlusOneSeparates() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new CountAggregate(), ts);
        op.processElement(e("a", 0));
        op.processElement(e("a", 11)); // 11 > 10：不合并
        op.advanceWatermark(30);
        Assert.assertEquals(2, op.changelog().size(), "two sessions at gap+1");
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 0, 0, 1);
        assertUpdate(op.changelog().get(1), ResultKind.ADD, "a", 11, 11, 1);
    }

    // ---------------------------------------------------------------
    // 验收：乱序 + 迟到事件桥接两个已封存窗口（撤回 + 新增）
    // ---------------------------------------------------------------

    @Test("乱序到达不影响准时窗口结果")
    static void unorderedOnTimeEvents() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new CountAggregate(), ts);
        op.processElement(e("a", 11));
        op.processElement(e("a", 1));  // 乱序，但 W=MIN 仍准时
        op.processElement(e("a", 6));
        op.advanceWatermark(31);
        Assert.assertEquals(1, op.changelog().size(), "out-of-order but on-time merges");
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 1, 11, 3);
    }

    @Test("迟到桥接事件撤回两个已封存窗口并新增合并窗口")
    static void lateEventBridgesTwoSealedWindows() {
        SimTimerService ts = new SimTimerService();
        // gap=10, L=20
        SessionWindowOperator op = new SessionWindowOperator(10, 20, new SumAggregate(), ts);

        // 第一段会话 [1,5]，W=15 封存（sealAt=5+10=15，闭区间）
        op.processElement(e("a", 1, 10));
        op.processElement(e("a", 5, 10));
        op.advanceWatermark(15);
        // 第二段会话 [21,25]：与第一段间隔 16 > 10，独立；W=35 封存（sealAt=35）
        op.processElement(e("a", 21, 100));
        op.processElement(e("a", 25, 100));
        op.advanceWatermark(35);
        Assert.assertEquals(2, op.changelog().size(), "two sealed sessions before bridge");
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 1, 5, 20);
        assertUpdate(op.changelog().get(1), ResultKind.ADD, "a", 21, 25, 200);
        Assert.assertEquals(2, op.sealedWindowCount("a"), "both retained for allowed lateness");

        // 迟到事件 t=15：距 5 和 21 都是 10（<=gap），桥接两个已封存窗口。
        // 门控：W-L=15，闭区间 t>=15，被接受。
        op.processElement(e("a", 15, 1000));

        Assert.assertEquals(5, op.changelog().size(), "retract two + add one => 5 updates");
        assertUpdate(op.changelog().get(2), ResultKind.RETRACT, "a", 1, 5, 20);
        assertUpdate(op.changelog().get(3), ResultKind.RETRACT, "a", 21, 25, 200);
        // 合并窗 sealAt=25+10=35 <= W=35，立即封存并 ADD
        assertUpdate(op.changelog().get(4), ResultKind.ADD, "a", 1, 25, 1220);
        Assert.assertEquals(1, op.sealedWindowCount("a"), "one merged sealed window retained");
        Assert.assertEquals(0, op.activeWindowCount("a"), "merged window seals immediately");

        // 超过允许迟到后，合并窗被清除（purgeAt=55，W>55 才清除）
        op.advanceWatermark(55);
        Assert.assertEquals(1, op.sealedWindowCount("a"), "W=purgeAt retains (purge is strict >)");
        op.advanceWatermark(56);
        Assert.assertEquals(0, op.sealedWindowCount("a"), "merged window purged after W>purgeAt");
        Assert.assertEquals(0, op.totalRetainedWindowCount(), "all state cleaned");
        Assert.assertEquals(5, op.changelog().size(), "purging emits no changelog records");
    }

    @Test("迟到事件桥接一个已封存窗口与一个活动窗口")
    static void lateEventBridgesSealedAndActive() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 30, new SumAggregate(), ts);

        op.processElement(e("a", 1, 10));
        op.advanceWatermark(11); // 封存 [1,1]（sealAt=11，闭区间）
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 1, 1, 10);

        // 活动窗口 [21,21]（sealAt=31，W=11 未封）
        op.processElement(e("a", 21, 20));
        Assert.assertEquals(1, op.activeWindowCount("a"), "one active window");

        // 迟到桥接 t=11：距 1 为 10（<=gap）、距 21 为 10（<=gap）；门控 11-30<11，接受
        op.processElement(e("a", 11, 5));
        Assert.assertEquals(2, op.changelog().size(), "one retract only (active had no ADD)");
        assertUpdate(op.changelog().get(1), ResultKind.RETRACT, "a", 1, 1, 10);
        Assert.assertEquals(1, op.activeWindowCount("a"), "merged window still active");
        Assert.assertEquals(0, op.sealedWindowCount("a"), "sealed one absorbed");

        op.advanceWatermark(31); // 合并窗 [1,21] sealAt=31，封存并 ADD（闭区间）
        Assert.assertEquals(3, op.changelog().size(), "final ADD emitted");
        assertUpdate(op.changelog().get(2), ResultKind.ADD, "a", 1, 21, 35);
    }

    // ---------------------------------------------------------------
    // 验收：封窗后迟到——门控边界、清除后迟到被丢弃
    // ---------------------------------------------------------------

    @Test("封窗后、允许迟到内的事件仍可并入；门控为闭区间")
    static void lateWithinAllowedLatenessAcceptedAtBoundary() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 5, new CountAggregate(), ts);
        op.processElement(e("a", 0));
        op.advanceWatermark(10); // [0,0] sealAt=10，闭区间封存
        Assert.assertEquals(1, op.changelog().size(), "sealed once");

        op.processElement(e("a", 15)); // W-L=5，15 被接受；15-0=15>10 不触及旧窗
        Assert.assertEquals(0, op.droppedLateEvents(), "boundary event accepted");
        // 15 与 [0,0] 不触及，独立成窗
        Assert.assertEquals(1, op.activeWindowCount("a"), "late event starts own window");

        op.advanceWatermark(20); // 门控 W-L=15
        op.processElement(e("a", 14)); // 14<15：丢弃
        Assert.assertEquals(1, op.droppedLateEvents(), "event below W-L dropped");
    }

    @Test("窗口清除（超过允许迟到）后的迟到事件被丢弃并计数")
    static void lateAfterPurgeDropped() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 5, new CountAggregate(), ts);
        op.processElement(e("a", 0));
        op.advanceWatermark(30); // sealAt=10 封存；purgeAt=15，W=30 已清除
        Assert.assertEquals(0, op.sealedWindowCount("a"), "sealed window purged");
        Assert.assertEquals(0, op.totalRetainedWindowCount(), "no retained state");

        long before = op.receivedEvents();
        op.processElement(e("a", 8)); // 8 < W-L=25：丢弃，无法再改结果
        Assert.assertEquals(1, op.droppedLateEvents(), "late event dropped after purge");
        Assert.assertEquals(before + 1, op.receivedEvents(), "dropped event still counted as received");
        Assert.assertEquals(1, op.changelog().size(), "dropped event creates no result change");
        assertUpdate(op.changelog().get(0), ResultKind.ADD, "a", 0, 0, 1);
    }

    @Test("允许迟到为 0：封窗后事件一律不再影响已封存窗口")
    static void zeroAllowedLateness() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new CountAggregate(), ts);
        op.processElement(e("a", 0));
        op.advanceWatermark(11); // 封存 [0,0]，L=0，purgeAt=10，已清除
        op.processElement(e("a", 5)); // 5<11：丢弃
        Assert.assertEquals(1, op.droppedLateEvents(), "L=0 drops everything behind W");
        Assert.assertEquals(0, op.sealedWindowCount("a"), "nothing retained with L=0");
    }

    // ---------------------------------------------------------------
    // 多 key 隔离
    // ---------------------------------------------------------------

    @Test("不同 key 的窗口状态互相隔离（不要求跨键的 changelog 顺序）")
    static void perKeyIsolation() {
        SimTimerService ts = new SimTimerService();
        SessionWindowOperator op = new SessionWindowOperator(10, 0, new CountAggregate(), ts);
        op.processElement(e("a", 0));
        op.processElement(e("b", 0));
        op.processElement(e("a", 5));
        op.advanceWatermark(20);
        Assert.assertEquals(2, op.changelog().size(), "one window per key");
        WindowUpdate aUpdate = op.changelog().stream()
                .filter(u -> u.key().equals("a")).findFirst().orElseThrow();
        WindowUpdate bUpdate = op.changelog().stream()
                .filter(u -> u.key().equals("b")).findFirst().orElseThrow();
        assertUpdate(aUpdate, ResultKind.ADD, "a", 0, 5, 2);
        assertUpdate(bUpdate, ResultKind.ADD, "b", 0, 0, 1);
    }

    // ---------------------------------------------------------------
    // 传递合并：桥接链
    // ---------------------------------------------------------------

    @Test("一个事件经传递链同时并入多个窗口")
    static void transitiveChainMerge() {
        SimTimerService ts = new SimTimerService();
        // gap=10,L=100，保留全部已封存窗口
        SessionWindowOperator op = new SessionWindowOperator(10, 100, new SumAggregate(), ts);
        op.processElement(e("a", 0, 1));    // 窗1 [0,0]
        op.advanceWatermark(10);           // sealAt=10：封存
        op.processElement(e("a", 15, 2));  // 窗2 [15,15]（与窗1间隔15>10）
        op.advanceWatermark(25);           // sealAt=25：封存
        op.processElement(e("a", 30, 4));  // 窗3 [30,30]
        op.advanceWatermark(40);           // sealAt=40：封存
        Assert.assertEquals(3, op.changelog().size(), "three sealed windows");

        // 事件 t=10：触及窗1（0..10，边界）与窗2（5..25，15-10=5<=10），合并二者；
        // 扩张窗 [0,15] 与窗3 [30,30] 间隔 30-15=15>10，不合并。
        op.processElement(e("a", 10, 8));
        Assert.assertEquals(6, op.changelog().size(), "retract x2 + add merged => 6");
        assertUpdate(op.changelog().get(5), ResultKind.ADD, "a", 0, 15, 11);
        // 合并窗 sealAt=25 <= W=25：立即封存；窗3 仍独立封存
        Assert.assertEquals(2, op.sealedWindowCount("a"), "[0,15] and [30,30]");

        // t=20：把 [0,15]（end+gap=25>=20）与 [30,30]（start-gap=20<=20，边界）桥接成链
        op.processElement(e("a", 20, 16));
        // 撤回 [0,15]=11、[30,30]=4，新增 [0,30]=31
        assertUpdate(op.changelog().get(6), ResultKind.RETRACT, "a", 0, 15, 11);
        assertUpdate(op.changelog().get(7), ResultKind.RETRACT, "a", 30, 30, 4);
        assertUpdate(op.changelog().get(8), ResultKind.ADD, "a", 0, 30, 31);
        Assert.assertEquals(1, op.sealedWindowCount("a"), "everything chained into one window");
    }

    // ---------------------------------------------------------------
    // 辅助
    // ---------------------------------------------------------------

    private static Event e(String key, long ts) {
        return new Event(key, ts, 1);
    }

    private static Event e(String key, long ts, long value) {
        return new Event(key, ts, value);
    }

    private static void assertUpdate(WindowUpdate u, ResultKind kind, String key,
                                     long start, long end, long agg) {
        Assert.assertEquals(kind, u.kind(), "kind");
        Assert.assertEquals(key, u.key(), "key");
        Assert.assertEquals(start, u.window().start(), "window start");
        Assert.assertEquals(end, u.window().end(), "window end");
        Assert.assertEquals(agg, u.aggregate(), "aggregate");
    }
}

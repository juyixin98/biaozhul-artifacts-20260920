package drvb.tests;

import drvb.json.Json;
import drvb.model.Event;
import drvb.model.ProcessResult;
import drvb.model.RejectReason;
import drvb.service.DynamicRuleService;
import drvb.time.ManualClock;
import drvb.time.ManualScheduler;
import drvb.version.RuleVersion;

import java.util.List;
import java.util.Map;

import static drvb.tests.Asserts.assertEquals;
import static drvb.tests.Asserts.assertFalse;
import static drvb.tests.Asserts.assertTrue;

/**
 * 验收场景：交错规则更新 × 乱序事件 × 边界时刻 × 回滚 × 历史版本回收前提。
 *
 * <p>规则（amount 阈值）：
 * <pre>
 *   事件时间区间            绑定   版本   阈值
 *   [0,      1000)        #1     v1     &gt;=100
 *   [1000,   3000)        #2     v2     &gt;=200  （收紧）
 *   [3000,   ∞)           #3     v1     &gt;=100  （回滚 v1）
 * </pre>
 * 处理时间（到达顺序）与事件时间刻意不同，制造乱序 / 晚到。
 * allowedLateness=100，retentionHorizon=5000。
 */
public class AcceptanceScenarioTest {

    private DynamicRuleService svc;
    private ManualClock clock;

    private static Map<String, Object> spec(double threshold) {
        return Json.asObject(Json.parse(
                "{\"op\":\"gte\",\"field\":\"amount\",\"value\":" + threshold + "}"));
    }

    private static Event ev(String id, long eventTime, double amount) {
        return new Event(id, eventTime, "payment", Map.of("amount", amount));
    }

    private void setUp() {
        clock = new ManualClock(90_000L);
        svc = new DynamicRuleService(clock, new ManualScheduler(clock), 100L, 5_000L);
        svc.publishVersion("v1", "threshold-100", 0L, spec(100), "初版阈值 100");
        svc.publishVersion("v2", "threshold-200", 1000L, spec(200), "收紧到 200");
        svc.rollback("v1", 3000L, "v2 误杀正常交易，回滚");
    }

    private ProcessResult send(long arrivalTime, Event e) {
        clock.advanceTo(arrivalTime);
        return svc.submit(e);
    }

    @Test
    public void interleavedUpdatesAndOutOfOrderEventsUseHistoricalVersions() {
        setUp();

        // 1) 到达顺序与事件时间乱序：先到一个 eventTime=2500 的事件（v2 区间）
        ProcessResult r1 = send(90_100L, ev("a", 2500L, 150));
        assertFalse(r1.matched(), "amount=150 在 v2(>=200) 下被过滤");
        assertEquals("v2", r1.ruleVersionId(), "2500 命中 v2");
        assertFalse(r1.late(), "第一条事件不晚到");
        assertEquals(2500L - 100L, r1.watermarkAfter(), "WM=maxET 2500 - 100 = 2400");

        // 2) 晚到事件 eventTime=500（v1 区间）：amount=150 在 v1 下应命中
        ProcessResult r2 = send(90_200L, ev("late-in-v1", 500L, 150));
        assertEquals("v1", r2.ruleVersionId(), "晚到事件按事件时间取历史版本 v1");
        assertTrue(r2.matched(), "v1 阈值 100：150 命中（若错用 v2 会被过滤）");
        assertTrue(r2.late(), "500 < WM 2400，标记为晚到");
        assertEquals(2400L, r2.watermarkAfter(), "晚到事件不推进水位线");

        // 3) 边界时刻 eventTime=1000 恰好属于 v2（左闭右开）
        ProcessResult r3 = send(90_300L, ev("edge-1000", 1000L, 100));
        assertEquals("v2", r3.ruleVersionId(), "边界 1000 属于 v2");
        assertFalse(r3.matched(), "v2 下 100 不命中");
        assertTrue(r3.late(), "1000 < 2400，晚到");

        // 4) 边界时刻 eventTime=3000 恰好属于回滚后的 v1
        ProcessResult r4 = send(90_400L, ev("edge-3000", 3000L, 150));
        assertEquals("v1", r4.ruleVersionId(), "边界 3000 属于回滚后的 v1");
        assertTrue(r4.matched(), "v1 下 150 命中");
        assertFalse(r4.late(), "3000 推进水位线，非晚到");
        assertEquals(3000L - 100L, r4.watermarkAfter(), "WM=2900");

        // 5) 区间内侧点确认
        ProcessResult r5 = send(90_500L, ev("in-v2", 2999L, 250));
        assertEquals("v2", r5.ruleVersionId(), "2999 仍在 v2 区间");
        assertTrue(r5.matched(), "v2 下 250 命中");
    }

    @Test
    public void missingVersionIsRejectedNeverSilentlyLatest() {
        setUp();
        // 第一条绑定 effectiveFrom=0；事件时间为负 = 任何版本之前
        ProcessResult r = send(90_100L, ev("ancient", -1L, 999));
        assertTrue(r.rejected(), "无覆盖版本 -> 拒绝");
        assertEquals(RejectReason.MISSING_RULE_VERSION, r.rejectReason(),
                "缺失版本拒绝，绝不静默套用最新规则");

        // 换一个空表场景：没有任何版本时，一切事件都拒绝
        DynamicRuleService empty = new DynamicRuleService(new ManualClock(0L),
                new ManualScheduler(clock), 0L, 0L);
        ProcessResult r2 = empty.submit(ev("nothing", 100L, 1));
        assertEquals(RejectReason.MISSING_RULE_VERSION, r2.rejectReason(), "空版本表同样拒绝");
    }

    @Test
    public void rollbackAndGarbageCollectionPreconditions() {
        setUp();

        // v1 的区间：[0,1000) 与 [3000,∞)；v2 的区间：[1000,3000)
        // v1 含未闭合区间 -> 永不满足 GC；v2 区间结束于 3000
        assertFalse(svc.eligibility("v1").eligible(), "v1 是当前版本，永不回收");
        assertFalse(svc.eligibility("v2").eligible(), "初始无水位线，v2 不可回收");

        // 把事件时间推进到 8000：WM=7900，gate=7900-5000=2900 < v2 区间结束 3000
        send(91_000L, ev("push-8000", 8000L, 50));
        assertFalse(svc.eligibility("v2").eligible(), "gate=2900：v2 尚不满足前提");
        assertEquals(2900L, svc.reclaimGate(), "闸门 2900");

        // 到达边界：事件时间 8100 -> WM=8000, gate=3000 == 区间结束 -> 可回收
        send(92_000L, ev("push-8100", 8100L, 50));
        assertTrue(svc.eligibility("v2").eligible(), "gate=3000 恰好等于区间结束，前提满足");

        // 回收前：v2 区间的晚到事件仍然可用历史版本
        ProcessResult before = send(92_100L, ev("late-v2-before-gc", 1500L, 250));
        assertEquals("v2", before.ruleVersionId(), "回收前 v2 晚到事件正常用 v2");
        assertTrue(before.matched(), "250 命中 v2");
        assertTrue(before.late(), "1500 晚到");

        // 执行回收
        List<String> reclaimed = svc.reclaimEligible();
        assertEquals(1, reclaimed.size(), "只有 v2 满足前提");
        assertEquals("v2", reclaimed.get(0), "回收 v2");

        // 回收后：同一历史区间的更晚到达事件被明确拒绝（不是改判 v1）
        ProcessResult after = send(92_200L, ev("late-v2-after-gc", 1600L, 250));
        assertTrue(after.rejected(), "回收后晚到事件被拒绝");
        assertEquals(RejectReason.RECLAIMED_RULE_VERSION, after.rejectReason(),
                "拒绝原因是版本已回收");

        // 当前区间（3000+）仍由 v1 正常服务
        ProcessResult current = send(92_300L, ev("cur", 9000L, 150));
        assertEquals("v1", current.ruleVersionId(), "当前区间 v1 不受 v2 回收影响");
        assertTrue(current.matched(), "v1: 150 命中");
    }

    @Test
    public void publishedVersionsAreImmutableObjects() {
        setUp();
        RuleVersion v1 = svc.table().snapshot().aliveVersions().get("v1");
        // 谓词规约是不可变副本
        boolean threw = false;
        try {
            v1.predicateSpec().put("hack", 1L);
        } catch (UnsupportedOperationException e) {
            threw = true;
        }
        assertTrue(threw, "已发布版本的谓词规约不可修改");
        // 重新求值结果稳定
        assertTrue(v1.matches(Map.of("amount", 150)), "v1 始终阈值 100");
        assertFalse(v1.matches(Map.of("amount", 50)), "v1 判定稳定");
    }

    @Test
    public void resultQueryClassifiesStatus() {
        setUp();
        send(90_100L, ev("m1", 3500L, 150));   // v1 命中
        send(90_200L, ev("f1", 3600L, 50));    // v1 未命中
        send(90_300L, ev("x1", -5L, 50));      // 缺失版本拒绝

        List<ProcessResult> matched =
                svc.queryResults(drvb.stream.EventProcessor.ResultFilter
                        .of(drvb.stream.EventProcessor.ResultStatus.MATCHED));
        assertEquals(1, matched.size(), "MATCHED 查询 1 条");
        assertEquals("m1", matched.get(0).eventId(), "命中事件 id");

        List<ProcessResult> rejected =
                svc.queryResults(drvb.stream.EventProcessor.ResultFilter
                        .of(drvb.stream.EventProcessor.ResultStatus.REJECTED));
        assertEquals(1, rejected.size(), "REJECTED 查询 1 条");
        assertEquals("x1", rejected.get(0).eventId(), "拒绝事件 id");
    }
}

package drvb.tests;

import drvb.json.Json;
import drvb.model.Event;
import drvb.model.ProcessResult;
import drvb.model.RejectReason;
import drvb.service.DynamicRuleService;
import drvb.time.Clock;
import drvb.time.ManualClock;
import drvb.time.ManualScheduler;
import drvb.version.VersionException;

import java.util.List;
import java.util.Map;

import static drvb.tests.Asserts.assertEquals;
import static drvb.tests.Asserts.assertFalse;
import static drvb.tests.Asserts.assertThrows;
import static drvb.tests.Asserts.assertTrue;

public class ReclamationTest {

    private static Map<String, Object> spec(double threshold) {
        return Json.asObject(Json.parse(
                "{\"op\":\"gte\",\"field\":\"amount\",\"value\":" + threshold + "}"));
    }

    private static Event event(String id, long eventTime, double amount) {
        return new Event(id, eventTime, "payment", Map.of("amount", amount));
    }

    private static DynamicRuleService service(ManualClock clock,
                                              long allowedLateness,
                                              long retentionHorizon) {
        return new DynamicRuleService(clock, new ManualScheduler(clock),
                allowedLateness, retentionHorizon);
    }

    @Test
    public void currentVersionCanNeverBeReclaimed() {
        ManualClock clock = new ManualClock(0L);
        DynamicRuleService svc = service(clock, 0L, 0L);
        svc.publishVersion("v1", "v1", 0L, spec(100), null);
        svc.publishVersion("v2", "v2", 1000L, spec(200), null);
        // 即使水位线推得很远，v2 有无限区间，不可回收
        svc.submit(event("e", 50_000L, 500));
        VersionException e = (VersionException) assertThrows(VersionException.class,
                () -> svc.reclaim("v2"), "当前版本不可回收");
        assertEquals("RECLAIM_NOT_ELIGIBLE", e.code(), "错误码");
    }

    @Test
    public void zeroHorizonGateIsExactlyTheBoundary() {
        ManualClock clock = new ManualClock(10_000L);
        DynamicRuleService svc = service(clock, 0L, 0L);
        svc.publishVersion("v1", "v1", 0L, spec(100), null);
        svc.publishVersion("v2", "v2", 1000L, spec(200), null);

        // 还没有事件 -> 水位线未初始化 -> 不可回收
        assertFalse(svc.eligibility("v1").eligible(), "无水位线时不可回收");

        svc.submit(event("e999", 999L, 150));
        // WM=999 < 区间结束 1000 -> 前提不满足
        assertFalse(svc.eligibility("v1").eligible(), "WM=999 时 v1 尚不可回收");

        svc.submit(event("e1000", 1000L, 250));
        // WM=1000 == 区间结束 1000，horizon=0 -> 恰好满足（<= 边界判定）
        assertTrue(svc.eligibility("v1").eligible(), "WM 恰好到达区间结束边界，可回收");

        List<String> reclaimed = svc.reclaimEligible();
        assertEquals(1, reclaimed.size(), "自动扫描只回收 v1");
        assertEquals("v1", reclaimed.get(0), "回收的是 v1");

        // 回收后，v1 区间的晚到事件被明确拒绝，而不是套用 v2
        ProcessResult late = svc.submit(event("late", 500L, 150));
        assertTrue(late.rejected(), "晚到事件被拒绝");
        assertEquals(RejectReason.RECLAIMED_RULE_VERSION, late.rejectReason(),
                "拒绝原因为版本已回收，未静默套用最新 v2");
    }

    @Test
    public void retentionHorizonDelaysReclamation() {
        ManualClock clock = new ManualClock(0L);
        DynamicRuleService svc = service(clock, 0L, 5_000L);
        svc.publishVersion("v1", "v1", 0L, spec(100), null);
        svc.publishVersion("v2", "v2", 1000L, spec(200), null);

        svc.submit(event("e5999", 5_999L, 500));
        assertFalse(svc.eligibility("v1").eligible(), "WM=5999, gate=999 < end=1000，不可回收");
        assertEquals(999L, svc.reclaimGate(), "回收闸门 = 5999 - 5000 = 999");

        svc.submit(event("e6000", 6_000L, 500));
        assertTrue(svc.eligibility("v1").eligible(), "WM=6000, gate=1000 == end，边界可回收");
        assertEquals(1000L, svc.reclaimGate(), "回收闸门恰好 1000");
        svc.reclaim("v1");
        assertTrue(svc.table().isReclaimed("v1"), "v1 已回收");
    }

    @Test
    public void rollbackCreatesMultipleIntervalsAllMustCloseBeforeReclaim() {
        ManualClock clock = new ManualClock(0L);
        DynamicRuleService svc = service(clock, 0L, 0L);
        svc.publishVersion("v1", "v1", 0L, spec(100), null);
        svc.publishVersion("v2", "v2", 1000L, spec(200), null);
        svc.rollback("v1", 2000L, "回滚 v1");
        // v1 现在拥有 [0,1000) 与 [2000,∞)：因包含未闭合区间，不可回收
        assertFalse(svc.eligibility("v1").eligible(), "回滚后 v1 成为当前版本，不可回收");
        // v2 的区间 [1000,2000) 已闭合，水位线超过 2000 后 v2 可回收
        svc.submit(event("e", 3000L, 50));
        assertTrue(svc.eligibility("v2").eligible(), "v2 唯一区间已闭合且过期，可回收");
        svc.reclaim("v2");
        // v2 被回收后，2500 之前那个区间的晚到事件遭拒绝；3000 的当前事件走 v1
        ProcessResult lateV2 = svc.submit(event("late", 1500L, 250));
        assertEquals(RejectReason.RECLAIMED_RULE_VERSION, lateV2.rejectReason(),
                "v2 区间晚到事件 -> 已回收拒绝");
        ProcessResult current = svc.submit(event("cur", 2500L, 50));
        assertEquals("v1", current.ruleVersionId(), "2500 走回滚后的 v1");
        assertFalse(current.matched(), "v1 阈值 100：amount=50 不命中");
    }

    @Test
    public void reclaimUnknownVersionFailsCleanly() {
        ManualClock clock = new ManualClock(0L);
        DynamicRuleService svc = service(clock, 0L, 0L);
        VersionException e = (VersionException) assertThrows(VersionException.class,
                () -> svc.reclaim("nope"), "不存在的版本");
        assertEquals("RECLAIM_NOT_ELIGIBLE", e.code(), "以前提不满足拒绝");
    }
}

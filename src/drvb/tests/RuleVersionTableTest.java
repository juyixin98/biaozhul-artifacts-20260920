package drvb.tests;

import drvb.json.Json;
import drvb.time.ManualClock;
import drvb.version.Binding;
import drvb.version.RuleVersion;
import drvb.version.RuleVersionTable;
import drvb.version.VersionException;

import java.util.Map;

import static drvb.tests.Asserts.assertEquals;
import static drvb.tests.Asserts.assertFalse;
import static drvb.tests.Asserts.assertThrows;
import static drvb.tests.Asserts.assertTrue;

public class RuleVersionTableTest {

    private static Map<String, Object> spec(String json) {
        return Json.asObject(Json.parse(json));
    }

    private static RuleVersion v(String id, long createdAt) {
        return new RuleVersion(id, id, createdAt,
                spec("{\"op\":\"gte\",\"field\":\"amount\",\"value\":100}"));
    }

    @Test
    public void boundaryTimestampsResolveExactInterval() {
        ManualClock clock = new ManualClock(0L);
        RuleVersionTable t = new RuleVersionTable(clock);
        t.publish(v("v1", 1), 100L, null);
        t.publish(v("v2", 2), 200L, null);

        // 左闭右开：100 属于 v1，199 属于 v1，200 恰好属于 v2
        assertEquals("v1", t.resolve(100L).version().id(), "下边界闭区间 100 -> v1");
        assertEquals("v1", t.resolve(199L).version().id(), "199 -> v1");
        assertEquals("v2", t.resolve(200L).version().id(), "边界时刻 200 -> v2");
        assertEquals("v2", t.resolve(999L).version().id(), "最后一个区间向右无限");
        assertEquals(2L, t.resolve(200L).binding().seq(), "200 命中的是第 2 条绑定");
    }

    @Test
    public void eventBeforeFirstBindingIsRejectedNotSilentlyLatest() {
        RuleVersionTable t = new RuleVersionTable(new ManualClock(0L));
        t.publish(v("v1", 1), 100L, null);
        t.publish(v("v2", 2), 200L, null);
        VersionException e = (VersionException) assertThrows(VersionException.class,
                () -> t.resolve(99L), "早于首边界必须拒绝");
        assertEquals("MISSING_RULE_VERSION", e.code(), "错误码为缺失版本而非套用 v2");
        VersionException e0 = (VersionException) assertThrows(VersionException.class,
                () -> t.resolve(0L), "0 时刻同样拒绝");
        assertEquals("MISSING_RULE_VERSION", e0.code(), "0 时刻错误码一致");
    }

    @Test
    public void publishRequiresStrictlyIncreasingBoundary() {
        RuleVersionTable t = new RuleVersionTable(new ManualClock(0L));
        t.publish(v("v1", 1), 100L, null);
        VersionException e = (VersionException) assertThrows(VersionException.class,
                () -> t.publish(v("v2", 2), 100L, null), "相同边界拒绝");
        assertEquals("BAD_EFFECTIVE_FROM", e.code(), "边界冲突错误码");
        assertThrows(VersionException.class,
                () -> t.publish(v("v3", 3), 50L, null), "倒退边界拒绝");
        assertThrows(VersionException.class,
                () -> t.publish(v("v1", 1), 300L, null), "重复版本 id 拒绝");
    }

    @Test
    public void rollbackAppendsBindingAndKeepsHistoryImmutable() {
        ManualClock clock = new ManualClock(1000L);
        RuleVersionTable t = new RuleVersionTable(clock);
        t.publish(v("v1", 1), 100L, null);
        t.publish(v("v2", 2), 200L, null);
        // 回滚到 v1：自 300 起 v1 重新生效，但 [100,200) 与 [200,300) 历史不变
        Binding rb = t.rollback("v1", 300L, "v2 有问题，回滚");
        assertEquals("ROLLBACK", rb.operation(), "操作类型为 ROLLBACK");
        assertEquals(3L, rb.seq(), "回滚是第 3 条绑定");
        assertEquals("v1", t.resolve(150L).version().id(), "历史区间 150 仍是 v1");
        assertEquals("v2", t.resolve(250L).version().id(), "历史区间 250 仍是 v2");
        assertEquals("v1", t.resolve(300L).version().id(), "回滚边界 300 起为 v1");

        assertThrows(VersionException.class, () -> t.rollback("v1", 400L, null),
                "回滚到当前相同版本拒绝");
        assertThrows(VersionException.class, () -> t.rollback("ghost", 400L, null),
                "回滚到不存在版本拒绝");
        assertFalse(t.isReclaimed("v2"), "v2 未被回收，仍可服务 250 的晚到事件");
    }

    @Test
    public void reclaimedVersionIsTombstoneAndRejectsLateEvents() {
        RuleVersionTable t = new RuleVersionTable(new ManualClock(0L));
        t.publish(v("v1", 1), 100L, null);
        t.publish(v("v2", 2), 200L, null);
        t.markReclaimed("v1");
        assertTrue(t.isReclaimed("v1"), "v1 成为墓碑");
        VersionException e = (VersionException) assertThrows(VersionException.class,
                () -> t.resolve(150L), "指向已回收版本的历史区间拒绝");
        assertEquals("RECLAIMED_RULE_VERSION", e.code(), "回收版本错误码");
        // 最新区间 v2 仍然正常
        assertEquals("v2", t.resolve(250L).version().id(), "v2 区间不受影响");
    }
}

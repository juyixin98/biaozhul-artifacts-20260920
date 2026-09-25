package streamagg.tests;

import java.math.BigDecimal;
import java.util.Map;

import streamagg.core.ApplyResult;
import streamagg.core.EventOp;
import streamagg.core.KeyStats;
import streamagg.core.OpType;
import streamagg.core.StreamProcessor;

/** 引擎核心行为测试：撤销先行、乱序缓存、更正、幂等、防负漂移、账本重算、重放。 */
public final class EngineTest {

    private static final String K = "k1";

    public static void main(String[] args) {
        TestFramework t = new TestFramework();

        testBasicAddAndRetract(t);
        testRetractBeforeAddNoVersion(t);
        testRetractBeforeAddVersioned(t);
        testMultipleCorrections(t);
        testOutOfOrderCorrectionsBuffered(t);
        testIdempotencyByOpId(t);
        testIdempotencySameVersion(t);
        testStaleConflictRejected(t);
        testNoNegativeDrift(t);
        testCorrectionMovesKey(t);
        testReconcileAndReplay(t);
        testReplayAfterBufferedScenario(t);
        testBigDecimalExact(t);
        testReAddAfterRetract(t);
        testRetractRetractAddIdempotency(t);

        int code = t.summary("EngineTest");
        if (code != 0) {
            System.exit(code);
        }
    }

    private static void testBasicAddAndRetract(TestFramework t) {
        t.test("基本：新增聚合后撤销归零", () -> {
            StreamProcessor p = new StreamProcessor();
            var r1 = p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            t.eq(r1.status(), ApplyResult.Status.APPLIED, "add 状态");
            t.eqKeyStats(p.statsOf(K), 1, "10", "add 后");
            p.submit(EventOp.add("e2", K, bd("5"), 1L, null));
            t.eqKeyStats(p.statsOf(K), 2, "15", "两个事件后");
            p.submit(EventOp.retract("e1", 2L, null));
            t.eqKeyStats(p.statsOf(K), 1, "5", "撤销 e1 后");
            p.submit(EventOp.retract("e2", 2L, null));
            t.eq(p.allStats().containsKey(K), false, "归零后键被移除");
            t.eqKeyStats(p.statsOf(K), 0, "0", "查询已消失键返回零");
        });
    }

    private static void testRetractBeforeAddNoVersion(TestFramework t) {
        t.test("先撤销后新增（无版本，合成版本缓存）", () -> {
            StreamProcessor p = new StreamProcessor();
            // 撤销先到：缓存
            var r1 = p.submit(EventOp.retract("e1", null, null));
            t.eq(r1.status(), ApplyResult.Status.BUFFERED, "撤销先到应缓存");
            t.eq(r1.version(), 2L, "撤销占 v2（v1 留给 ADD）");
            t.eqKeyStats(p.statsOf(K), 0, "0", "撤销先到不得产生负数");
            t.eq(p.pendingOf("e1").size(), 1, "挂起 1 条");
            // 新增后到：v1 应用，级联排空 v2 撤销
            var r2 = p.submit(EventOp.add("e1", K, bd("10"), null, null));
            t.eq(r2.status(), ApplyResult.Status.APPLIED, "新增到达后级联生效");
            t.eqKeyStats(p.statsOf(K), 0, "0", "新增-撤销级联后仍为 0");
            t.eq(p.ledger().size(), 0, "最终账本无存活事件");
            t.eq(p.pendingOf("e1").size(), 0, "挂起已排空");
            // 账本重算一致
            var recomputed = StreamProcessor.recomputeFromLedger(p.ledger());
            t.eq(recomputed.containsKey(K), false, "账本重算也为 0");
        });
    }

    private static void testRetractBeforeAddVersioned(TestFramework t) {
        t.test("先撤销后新增（显式版本乱序）", () -> {
            StreamProcessor p = new StreamProcessor();
            var r1 = p.submit(EventOp.retract("e1", 2L, null));
            t.eq(r1.status(), ApplyResult.Status.BUFFERED, "v2 撤销缓存");
            t.eqKeyStats(p.statsOf(K), 0, "0", "不允许负漂移");
            var r2 = p.submit(EventOp.add("e1", K, bd("100"), 1L, null));
            t.eq(r2.status(), ApplyResult.Status.APPLIED, "v1 新增到达");
            t.eqKeyStats(p.statsOf(K), 0, "0", "级联撤销后为 0");
        });
    }

    private static void testMultipleCorrections(TestFramework t) {
        t.test("多次更正：只保留最新值，计数不变", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            p.submit(EventOp.correct("e1", K, bd("12"), 2L, null));
            t.eqKeyStats(p.statsOf(K), 1, "12", "第一次更正");
            p.submit(EventOp.correct("e1", K, bd("15.5"), 3L, null));
            t.eqKeyStats(p.statsOf(K), 1, "15.5", "第二次更正");
            p.submit(EventOp.correct("e1", K, bd("-2"), 4L, null));
            t.eqKeyStats(p.statsOf(K), 1, "-2", "更正允许负值");
            // 撤销后账本重算
            p.submit(EventOp.retract("e1", 5L, null));
            t.eq(p.ledger().size(), 0, "撤销后账本空");
            var rr = p.reconcile();
            t.eq(rr.consistent(), true, "撤销后对账一致");
        });
    }

    private static void testOutOfOrderCorrectionsBuffered(TestFramework t) {
        t.test("更正乱序到达：缓存依赖、按版本补齐后级联", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            // v4 先到，v2、v3 缺失 -> 缓存
            var r4 = p.submit(EventOp.correct("e1", K, bd("40"), 4L, null));
            t.eq(r4.status(), ApplyResult.Status.BUFFERED, "v4 缓存");
            t.eqKeyStats(p.statsOf(K), 1, "10", "缓存期间聚合不变");
            var r3 = p.submit(EventOp.correct("e1", K, bd("30"), 3L, null));
            t.eq(r3.status(), ApplyResult.Status.BUFFERED, "v3 缓存");
            t.eqKeyStats(p.statsOf(K), 1, "10", "仍不应用");
            var r2 = p.submit(EventOp.correct("e1", K, bd("20"), 2L, null));
            t.eq(r2.status(), ApplyResult.Status.APPLIED, "v2 到达并级联 v3、v4");
            t.eqKeyStats(p.statsOf(K), 1, "40", "级联后取 v4 的值");
            t.eq(p.pendingOf("e1").size(), 0, "无挂起");
            var le = p.ledger().get(0);
            t.eq(le.version(), 4L, "账本记录最终版本 v4");
        });
    }

    private static void testIdempotencyByOpId(TestFramework t) {
        t.test("opId 幂等：重复提交整体忽略", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, "op-1"));
            var dup = p.submit(EventOp.add("e1", K, bd("10"), 1L, "op-1"));
            t.eq(dup.status(), ApplyResult.Status.DUPLICATE, "重复 opId 判定重复");
            t.eqKeyStats(p.statsOf(K), 1, "10", "重复不影响聚合");
            // 乱序缓存中的操作重放也幂等
            p.submit(EventOp.correct("e1", K, bd("20"), 3L, "op-3"));
            var dupBuffered = p.submit(EventOp.correct("e1", K, bd("20"), 3L, "op-3"));
            t.eq(dupBuffered.status(), ApplyResult.Status.DUPLICATE, "缓存中的重复也幂等");
            t.eq(p.journal().size(), 2, "重复不写日志");
        });
    }

    private static void testIdempotencySameVersion(TestFramework t) {
        t.test("同版本相同内容（无 opId）也幂等", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            var dup = p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            t.eq(dup.status(), ApplyResult.Status.DUPLICATE, "同版本同内容重复");
            t.eqKeyStats(p.statsOf(K), 1, "10", "幂等");
            // BigDecimal 数值相等（不同标度）也算重复
            var dup2 = p.submit(EventOp.add("e1", K, new BigDecimal("10.0"), 1L, null));
            t.eq(dup2.status(), ApplyResult.Status.DUPLICATE, "10 与 10.0 数值相等");
        });
    }

    private static void testStaleConflictRejected(TestFramework t) {
        t.test("同版本不同内容：陈旧冲突被拒绝", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            var bad = p.submit(EventOp.add("e1", K, bd("99"), 1L, null));
            t.eq(bad.status(), ApplyResult.Status.STALE_CONFLICT, "冲突拒绝");
            t.eqKeyStats(p.statsOf(K), 1, "10", "拒绝陈旧消息");
        });
    }

    private static void testNoNegativeDrift(TestFramework t) {
        t.test("禁止计数负漂移：撤销未知事件、重复撤销均安全", () -> {
            StreamProcessor p = new StreamProcessor();
            // 撤销完全不存在的事件（带版本）：缓存 v2 后，永远等不到 v1；直接重放不会负
            p.submit(EventOp.retract("ghost", 2L, null));
            t.eqKeyStats(p.statsOf(K), 0, "0", "未知事件撤销无影响");
            // 无版本撤销未知事件：缓存 v2，同样安全
            p.submit(EventOp.retract("ghost2", null, null));
            t.eqKeyStats(p.statsOf(K), 0, "0", "无版本未知撤销无影响");

            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            p.submit(EventOp.retract("e1", 2L, null));
            t.eqKeyStats(p.statsOf(K), 0, "0", "已撤销");
            // 再来一次撤销（无版本 -> 取后续合成版本），事件不存活，空操作
            var r3 = p.submit(EventOp.retract("e1", null, null));
            // 对已撤销链：无版本 RETRACT 取 firstFree>=? st.applied 非空 -> g=3，直接应用为空操作
            t.eq(r3.status(), ApplyResult.Status.APPLIED, "重复撤销被接收");
            t.eqKeyStats(p.statsOf(K), 0, "0", "重复撤销不产生负数");
        });
    }

    private static void testCorrectionMovesKey(TestFramework t) {
        t.test("更正可改键：旧键减、新键增，计数随事件迁移", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", "a", bd("10"), 1L, null));
            p.submit(EventOp.add("e2", "a", bd("5"), 1L, null));
            p.submit(EventOp.correct("e1", "b", bd("10"), 2L, null));
            t.eqKeyStats(p.statsOf("a"), 1, "5", "旧键 a");
            t.eqKeyStats(p.statsOf("b"), 1, "10", "新键 b");
            p.submit(EventOp.correct("e1", "b", bd("20"), 3L, null));
            t.eqKeyStats(p.statsOf("b"), 1, "20", "同键更正值");
            var rr = p.reconcile();
            t.eq(rr.consistent(), true, "迁移后账本重算一致");
        });
    }

    private static void testReconcileAndReplay(TestFramework t) {
        t.test("综合：多次更正+撤销+重放与账本重算一致", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, "a1"));
            p.submit(EventOp.add("e2", K, bd("20"), 1L, "a2"));
            p.submit(EventOp.correct("e1", K, bd("11"), 2L, "a3"));
            p.submit(EventOp.retract("e2", 2L, "a4"));
            p.submit(EventOp.correct("e1", K, bd("13"), 3L, "a5"));
            t.eqKeyStats(p.statsOf(K), 1, "13", "增量结果");

            // 账本重算（独立参考实现）
            Map<String, KeyStats> recomputed = StreamProcessor.recomputeFromLedger(p.ledger());
            t.eqKeyStats(recomputed.get(K), 1, "13", "账本重算结果");

            var reconcile = p.reconcile();
            t.eq(reconcile.consistent(), true, "对账一致");

            var replay = p.replay();
            t.eq(replay.consistent(), true, "重放一致");
        });
    }

    private static void testReplayAfterBufferedScenario(TestFramework t) {
        t.test("重放覆盖乱序缓存场景：先撤销后新增+多次更正", () -> {
            StreamProcessor p = new StreamProcessor();
            // 验收组合拳：v3 更正先到、撤销 v4 先到，再补 v1 ADD、v2 CORRECT
            p.submit(EventOp.correct("e1", K, bd("30"), 3L, null));
            p.submit(EventOp.retract("e1", 4L, null));
            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            p.submit(EventOp.correct("e1", K, bd("20"), 2L, null));
            t.eq(p.pendingOf("e1").size(), 0, "全部排空");
            t.eq(p.ledger().size(), 0, "最终撤销 -> 无存活");
            t.eqKeyStats(p.statsOf(K), 0, "0", "增量为零");
            var replay = p.replay();
            t.eq(replay.consistent(), true, "复杂乱序后重放一致");
            var rec = p.reconcile();
            t.eq(rec.consistent(), true, "复杂乱序后对账一致");
        });
    }

    private static void testBigDecimalExact(TestFramework t) {
        t.test("BigDecimal 精确：0.1+0.2 与长小数不丢精度", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("0.1"), 1L, null));
            p.submit(EventOp.add("e2", K, bd("0.2"), 1L, null));
            t.eqKeyStats(p.statsOf(K), 2, "0.3", "0.1+0.2 精确");
            p.submit(EventOp.add("e3", K, bd("123456789.123456789012345"), 1L, null));
            t.eqKeyStats(p.statsOf(K), 3, "123456789.423456789012345", "长小数精确");
            p.submit(EventOp.correct("e3", K, bd("0.000000000000005"), 2L, null));
            t.eq(p.statsOf(K).sum().compareTo(new BigDecimal("0.300000000000005")), 0,
                    "更正后长小数精确");
        });
    }

    private static void testReAddAfterRetract(TestFramework t) {
        t.test("撤销后重新新增（同事件 ID 复活）", () -> {
            StreamProcessor p = new StreamProcessor();
            p.submit(EventOp.add("e1", K, bd("10"), 1L, null));
            p.submit(EventOp.retract("e1", 2L, null));
            t.eqKeyStats(p.statsOf(K), 0, "0", "撤销");
            p.submit(EventOp.add("e1", K, bd("30"), 3L, null));
            t.eqKeyStats(p.statsOf(K), 1, "30", "重新新增");
            t.eq(p.ledger().size(), 1, "账本恢复 1 条");
            var rr = p.reconcile();
            t.eq(rr.consistent(), true, "复活后一致");
        });
    }

    private static void testRetractRetractAddIdempotency(TestFramework t) {
        t.test("无版本场景：撤销-撤销-新增-新增 全幂等不乱序", () -> {
            StreamProcessor p = new StreamProcessor();
            var r1 = p.submit(EventOp.retract("e1", null, "r1")); // 缓存 v2
            t.eq(r1.status(), ApplyResult.Status.BUFFERED, "首个撤销缓存");
            var r2 = p.submit(EventOp.retract("e1", null, "r2")); // 不同 opId -> v3 缓存
            t.eq(r2.status(), ApplyResult.Status.BUFFERED, "第二个撤销也缓存");
            var r1dup = p.submit(EventOp.retract("e1", null, "r1"));
            t.eq(r1dup.status(), ApplyResult.Status.DUPLICATE, "opId 重复");
            var add = p.submit(EventOp.add("e1", K, bd("10"), null, "add1")); // v1，级联 v2、v3
            t.eq(add.status(), ApplyResult.Status.APPLIED, "新增级联两个撤销");
            t.eqKeyStats(p.statsOf(K), 0, "0", "两次撤销都为空操作后仍为 0");
            // 再来 ADD（同事件复活）
            var add2 = p.submit(EventOp.add("e1", K, bd("5"), null, "add2"));
            t.eq(add2.status(), ApplyResult.Status.APPLIED, "复活新增");
            t.eqKeyStats(p.statsOf(K), 1, "5", "复活计数正确");
            var replay = p.replay();
            t.eq(replay.consistent(), true, "重放一致");
        });
    }

    private static BigDecimal bd(String s) {
        return new BigDecimal(s);
    }
}

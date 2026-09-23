package cep;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 匹配引擎核心语义测试，覆盖任务书的两个核心验收场景：
 *   testAcceptance1_doubleA:  A,A,B,C         -> 2 个匹配
 *   testAcceptance2_timeout: A,B,超时,C      -> 0 个匹配
 * 其余用例覆盖窗口边界、重叠匹配、跳过无关事件、实体分组、相同时间按输入序号、
 * 晚到拒绝、以及“禁止静默截断组合”的显式失败。
 */
public class EngineTest {

    private static Event e(String type, String entity, long ts) {
        return new Event(type, entity, ts, -1);
    }

    private static List<Event> list(Event... events) {
        return new ArrayList<>(Arrays.asList(events));
    }

    // ----------------------------------------------------------- 验收场景

    /** 验收 1：A,A,B,C 必须产生 2 个匹配（两个 A 都要与 B、C 组合，不得静默丢组合）。 */
    public void testAcceptance1_doubleA() {
        Engine engine = new Engine();
        Engine.IngestResult r = engine.ingest(list(
                e("A", "x", 1000),
                e("A", "x", 1000),
                e("B", "x", 2000),
                e("C", "x", 3000)));
        TestRunner.checkEq(r.created.size(), 2, "A,A,B,C 应产生 2 个匹配");
        TestRunner.checkEq(engine.totalMatches(), 2, "累计匹配数应为 2");
        // 验证两个匹配分别使用不同的 A（按输入序号 0 与 1）
        List<Match> ms = engine.queryMatches("x");
        TestRunner.checkEq(ms.get(0).a.seq, 0L, "第一个匹配的 A 应是输入序号 0");
        TestRunner.checkEq(ms.get(1).a.seq, 1L, "第二个匹配的 A 应是输入序号 1");
        TestRunner.check(ms.get(0).b.seq == 2 && ms.get(0).c.seq == 3,
                "B/C 事件序号应为 2/3");
    }

    /**
     * 验收 2：A,B,超时,C —— C 距 A 超过 10 秒，窗口关闭，必须 0 个匹配；
     * 同时验证超时后 A->B 部分匹配被清除，再来的 C 不会误配。
     */
    public void testAcceptance2_timeout() {
        Engine engine = new Engine();
        engine.ingest(list(e("A", "x", 0), e("B", "x", 1000)));
        Engine.IngestResult r = engine.ingest(list(e("C", "x", 11_000)));
        TestRunner.checkEq(r.created.size(), 0, "A 与 C 相隔 11 秒，超出 10 秒窗口，应 0 匹配");
        TestRunner.checkEq(engine.totalMatches(), 0, "累计匹配数应为 0");
        TestRunner.check(engine.viewState("x").waitingABs.isEmpty(),
                "超时后 A->B 部分匹配应已被清除");

        // 之后同实体再来一个窗口内的 A,B,C，应能正常匹配（旧状态不残留）。
        engine.ingest(list(
                e("A", "x", 20_000),
                e("B", "x", 20_500),
                e("C", "x", 20_900)));
        TestRunner.checkEq(engine.totalMatches(), 1, "超时不影响后续新匹配");
    }

    // ----------------------------------------------------------- 窗口边界

    /** 恰好 10.000 秒：命中；10.001 秒：不命中。 */
    public void testWindowBoundaryInclusive() {
        Engine exact = new Engine();
        exact.ingest(list(e("A", "x", 0), e("B", "x", 5000), e("C", "x", 10_000)));
        TestRunner.checkEq(exact.totalMatches(), 1, "C-A 恰好 10000ms 应命中");

        Engine over = new Engine();
        over.ingest(list(e("A", "x", 0), e("B", "x", 5000), e("C", "x", 10_001)));
        TestRunner.checkEq(over.totalMatches(), 0, "C-A 为 10001ms 不应命中");
    }

    // ----------------------------------------------------------- 重叠匹配

    /** A,B,B,C,C：两个 B 各自承接 A，两个 C 各自承接两个 A->B，共 4 个匹配。 */
    public void testOverlappingMatches() {
        Engine engine = new Engine();
        Engine.IngestResult r = engine.ingest(list(
                e("A", "x", 0),
                e("B", "x", 100),
                e("B", "x", 200),
                e("C", "x", 300),
                e("C", "x", 400)));
        TestRunner.checkEq(r.created.size(), 4, "A,B,B,C,C 应产生 4 个重叠匹配");
    }

    /** 一次匹配完成后，参与过的 A/B 仍可继续匹配后续 C（事件不被消费）。 */
    public void testEventsAreNotConsumed() {
        Engine engine = new Engine();
        engine.ingest(list(e("A", "x", 0), e("B", "x", 100), e("C", "x", 200)));
        Engine.IngestResult r2 = engine.ingest(list(e("C", "x", 300)));
        TestRunner.checkEq(r2.created.size(), 1, "同一 A->B 应能再次匹配窗口内的第二个 C");
    }

    // ----------------------------------------------------------- 跳过无关事件

    /** 非 A/B/C 的事件一律跳过：不产生匹配，也不破坏部分匹配状态。 */
    public void testIrrelevantEventsAreSkipped() {
        Engine engine = new Engine();
        Engine.IngestResult r = engine.ingest(list(
                e("X", "x", 0),
                e("A", "x", 100),
                e("PAUSE", "x", 200),
                e("B", "x", 300),
                e("X", "x", 400),
                e("C", "x", 500),
                e("X", "x", 600)));
        TestRunner.checkEq(r.created.size(), 1, "无关事件不应干扰 A->B->C 匹配");
        TestRunner.checkEq(engine.viewState("x").watermark, 600L,
                "无关事件仍应推进实体时间水位");
    }

    // ----------------------------------------------------------- 实体分组

    /** 不同实体之间绝不串配：x 的 B 不能配 y 的 A。 */
    public void testGroupsByEntity() {
        Engine engine = new Engine();
        engine.ingest(list(
                e("A", "x", 0),
                e("A", "y", 100),
                e("B", "x", 200),
                e("C", "y", 300))); // y 只有 A,C，没有 B -> 不匹配；x 有 A,B，没有 C -> 不匹配
        TestRunner.checkEq(engine.totalMatches(), 0, "跨实体不得产生匹配");
        engine.ingest(list(e("C", "x", 400)));
        TestRunner.checkEq(engine.totalMatches(), 1, "补齐 x 的 C 后应恰好 1 个匹配");
        TestRunner.checkEq(engine.queryMatches("x").size(), 1, "匹配属于实体 x");
        TestRunner.checkEq(engine.queryMatches("y").size(), 0, "实体 y 无匹配");
    }

    // ----------------------------------------------------- 相同时间/输入序号

    /** 相同时间戳按输入序号排序：B 在 A 之后输入才算先 A 后 B。 */
    public void testSameTimestampOrderingBySeq() {
        // 输入顺序 A,B,C（同一毫秒）：合法命中 1 个
        Engine forward = new Engine();
        forward.ingest(list(e("A", "x", 5000), e("B", "x", 5000), e("C", "x", 5000)));
        TestRunner.checkEq(forward.totalMatches(), 1, "同时间按输入序 A,B,C 应命中");

        // 输入顺序 B,A,C（同一毫秒）：B 到达时尚无存活 A，不命中
        Engine backward = new Engine();
        backward.ingest(list(e("B", "x", 5000), e("A", "x", 5000), e("C", "x", 5000)));
        TestRunner.checkEq(backward.totalMatches(), 0,
                "同时间按输入序 B,A,C 时 B 早于 A，不应命中");
    }

    /** 跨批次的输入序号也必须全局单调递增。 */
    public void testSeqIsGlobalAcrossBatches() {
        Engine engine = new Engine();
        engine.ingest(list(e("A", "x", 0), e("A", "x", 10)));
        engine.ingest(list(e("B", "x", 20)));
        engine.ingest(list(e("C", "x", 30)));
        List<Match> ms = engine.queryMatches("x");
        TestRunner.checkEq(ms.size(), 2, "跨批次的 A,A,B,C 仍应 2 匹配");
        TestRunner.checkEq(ms.get(0).b.seq, 2L, "第二批的 B 序号应接着前两事件为 2");
        TestRunner.checkEq(ms.get(0).c.seq, 3L, "第三批的 C 序号应为 3");
    }

    // ----------------------------------------------------------- 晚到拒绝

    /** 晚到事件必须显式报错（409 的引擎侧对应物），且状态保持批次原子不变。 */
    public void testLateEventRejectedAtomically() {
        Engine engine = new Engine();
        engine.ingest(list(e("A", "x", 5000)));
        boolean threw = false;
        try {
            engine.ingest(list(e("B", "x", 4000)));
        } catch (Engine.LateEventException ex) {
            threw = true;
            TestRunner.checkEq(ex.watermark, 5000L, "异常中应带水位");
        }
        TestRunner.check(threw, "晚到事件必须抛出 LateEventException");
        // 该批次整体不生效：水位未回退，晚到的 B 未入状态
        TestRunner.checkEq(engine.viewState("x").watermark, 5000L, "晚到批次不得改动水位");
        TestRunner.check(engine.viewState("x").waitingABs.isEmpty(),
                "晚到的 B 不得进入部分匹配");

        // 同批次包含多个事件时，其中一个晚到 -> 整批不生效（含前面看似合法的事件）
        try {
            engine.ingest(list(e("A", "y", 1), e("B", "x", 6000), e("B", "x", 4000)));
            TestRunner.fail("批次内晚到事件必须拒绝整批");
        } catch (Engine.LateEventException expected) {
            // 预期
        }
        TestRunner.check(engine.viewState("y").waitingAs.isEmpty(),
                "原子性：批次中晚到事件导致整批不生效，y 的 A 也不得落库");
    }

    // ------------------------------------------------- 禁止静默截断组合

    /**
     * 候选组合超过上限时必须显式失败，绝不静默只返回前 N 个：
     * 上限设为 1，A,A,B,C 对单个 C 有 2 个候选 -> CombinationLimitException，
     * 且引擎状态与匹配数完全不被该批次改动。
     */
    public void testCombinationLimitFailsLoudly() {
        Engine engine = new Engine(1);
        boolean threw = false;
        try {
            engine.ingest(list(
                    e("A", "x", 0),
                    e("A", "x", 100),
                    e("B", "x", 200),
                    e("C", "x", 300)));
        } catch (Engine.CombinationLimitException ex) {
            threw = true;
            TestRunner.checkEq(ex.candidateCount, 2, "异常应报告候选数 2");
            TestRunner.checkEq(ex.limit, 1, "异常应报告上限 1");
        }
        TestRunner.check(threw, "超过组合上限必须显式抛异常，而不是静默截断");
        TestRunner.checkEq(engine.totalMatches(), 0, "超限批次不得产生任何匹配");
        TestRunner.check(engine.viewState("x").waitingAs.isEmpty(),
                "超限批次不得改动任何状态（预检失败保持原子性）");

        // 引擎仍然可用：后续一个不超限的批次照常工作
        engine.ingest(list(e("A", "x", 1000), e("B", "x", 1100), e("C", "x", 1200)));
        TestRunner.checkEq(engine.totalMatches(), 1, "显式失败后引擎应可继续正常服务");
    }

    /** 上限内的重叠组合全部保留。 */
    public void testCombinationWithinLimitAllKept() {
        Engine engine = new Engine(100_000);
        int n = 50;
        List<Event> batch = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            batch.add(e("A", "x", i));
        }
        batch.add(e("B", "x", 100));
        batch.add(e("C", "x", 200));
        engine.ingest(batch);
        TestRunner.checkEq(engine.totalMatches(), n,
                "50 个 A 与单个 B/C 应产生完整的 50 个匹配，一个都不能少");
    }

    // ----------------------------------------------------------- 部分匹配视图

    public void testPartialStateView() {
        Engine engine = new Engine();
        engine.ingest(list(e("A", "x", 0)));
        engine.ingest(list(e("B", "x", 100)));
        Engine.EntityStateView v = engine.viewState("x");
        TestRunner.checkEq(v.waitingAs.size(), 1, "应有 1 个等待中的 A");
        TestRunner.checkEq(v.waitingABs.size(), 1, "应有 1 个等待中的 A->B");
        TestRunner.checkEq(v.watermark, 100L, "水位应为 100");
    }

    /** 超过窗口后只推进一个无关事件，也应清掉过期部分匹配。 */
    public void testPurgeTriggeredByIrrelevantEvent() {
        Engine engine = new Engine();
        engine.ingest(list(e("A", "x", 0), e("B", "x", 100)));
        engine.ingest(list(e("X", "x", 11_000)));
        Engine.EntityStateView v = engine.viewState("x");
        TestRunner.check(v.waitingAs.isEmpty() && v.waitingABs.isEmpty(),
                "无关事件推进时间后也应清除过期部分匹配");
    }
}

package tests;

import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.EngineResult;
import streammatch.model.Event;
import streammatch.model.LatePolicy;
import streammatch.model.Match;
import streammatch.model.MatchPolicy;
import streammatch.model.RemovedA;
import streammatch.engine.StreamMatcher;

import java.util.ArrayList;
import java.util.List;

/**
 * 验收核心：手算短序列。每个序列在 {@code docs/hand-computed.md} 中都有逐步推演，
 * 本类把同样的预期固化为断言。窗口统一 W=100ms，ALL_CANDIDATES 策略。
 */
public class HandComputedTest extends TestCase {

    private static final long W = 100L;

    private static Event e(String id, String type, long ts) {
        return new Event(id, "k", type, ts, 0L);
    }

    /** 只取匹配的 key|aId|bId 三元组列表，按发出顺序。 */
    private static List<String> pairs(List<Match> ms) {
        List<String> out = new ArrayList<>();
        for (Match m : ms) {
            out.add(pair(m.key(), m.aId(), m.bId()));
        }
        return out;
    }

    private static StreamMatcher newEngine() {
        return StreamMatcher.eventTime(
                new EngineConfig(EngineMode.EVENT_TIME, W, MatchPolicy.ALL_CANDIDATES, 0L, LatePolicy.DROP));
    }

    @Override
    protected void run() {
        case1Basic();
        case2MultipleA();
        case2bRepeatedMatch();
        case3CInterrupt();
        case3bCInterruptsAllBefore();
        case4Timeout();
        case4bExpireAfterMatch();
        case5SameTimestampOrder();
        case5bSameTimestampAB();
        case5cSameTimestampBFirst();
        case6Boundary();
        case7OutOfOrderLate();
        case8ReplaySorted();
        case9RejectLate();
        case10KeysIndependent();
    }

    // ---------------------------------------------------------------- 1
    // A1@0, B2@50  -> 一个匹配 (A1,B2)。
    private void case1Basic() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(e("A1", "A", 0), e("B2", "B", 50)));
        eq(pairs(r.matches()), List.of(pair("k", "A1", "B2")), "case1: A@0 B@50 匹配");
        eq(r.removed().size(), 0, "case1: 无移除");
        eq(r.lateDropped().size(), 0, "case1: 无迟到");
    }

    // ---------------------------------------------------------------- 2
    // A1@0, A2@10, B3@60 -> B 同时匹配 A1 与 A2（多 A 候选、重叠输出）。
    private void case2MultipleA() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(e("A1", "A", 0), e("A2", "A", 10), e("B3", "B", 60)));
        eq(pairs(r.matches()), List.of(pair("k", "A1", "B3"), pair("k", "A2", "B3")),
                "case2: 一个 B 匹配两个 A（按 A 的全序先后发出）");
    }

    // A1@0, A2@20, B3@50, B4@80：A1 与 A2 各匹配两个 B，共 4 个重叠匹配。
    private void case2bRepeatedMatch() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(
                e("A1", "A", 0), e("A2", "A", 20), e("B3", "B", 50), e("B4", "B", 80)));
        eq(pairs(r.matches()), List.of(
                        pair("k", "A1", "B3"), pair("k", "A2", "B3"),
                        pair("k", "A1", "B4"), pair("k", "A2", "B4")),
                "case2b: ALL_CANDIDATES 下 A 可重复匹配");
    }

    // ---------------------------------------------------------------- 3
    // C 打断：A1@0, C2@40, B3@60 -> 无匹配，A1 被 C2 打断。
    private void case3CInterrupt() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(e("A1", "A", 0), e("C2", "C", 40), e("B3", "B", 60)));
        eq(r.matches().size(), 0, "case3: C 打断后无匹配");
        eq(r.removed().size(), 1, "case3: 恰好一次移除");
        RemovedA rm = r.removed().get(0);
        eq(rm.aId(), "A1", "case3: 被移除的是 A1");
        eq(rm.reason().name(), "INTERRUPTED_BY_C", "case3: 原因为 C 打断");
        eq(rm.cId(), "C2", "case3: 记录打断者 C2");
    }

    // 两个候选 A，C 只打断更早的那个：A1@0, A2@30, C3@50, B4@90
    // C 在全序上位于 A2 之后（50>30），所以 A1、A2 都被打断；B4 无匹配。
    private void case3bCInterruptsAllBefore() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(
                e("A1", "A", 0), e("A2", "A", 30), e("C3", "C", 50), e("B4", "B", 90)));
        eq(r.matches().size(), 0, "case3b: 全部 A 在 C 之前，无匹配");
        eq(r.removed().size(), 2, "case3b: A1、A2 都被 C 打断");
    }

    // ---------------------------------------------------------------- 4
    // 超时：A1@0, B2@120 -> 已超窗口（严格 >100），无匹配；B2 把 watermark 推到 120，A1 超时。
    private void case4Timeout() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(e("A1", "A", 0), e("B2", "B", 120)));
        eq(r.matches().size(), 0, "case4: 窗口外 B 不匹配");
        eq(r.removed().size(), 1, "case4: A1 超时移除");
        eq(r.removed().get(0).reason().name(), "TIMEOUT", "case4: 原因 TIMEOUT");
    }

    // 先等到 B（成功匹配），窗口结束后 A 以 EXPIRED_AFTER_MATCH 退场。
    private void case4bExpireAfterMatch() {
        StreamMatcher m = newEngine();
        m.process(List.of(e("A1", "A", 0), e("B2", "B", 40)));
        EngineResult tail = m.advanceWatermark(101); // 严格大于 tA+W=100
        eq(tail.removed().size(), 1, "case4b: 匹配后仍到期");
        eq(tail.removed().get(0).reason().name(), "EXPIRED_AFTER_MATCH",
                "case4b: 原因 EXPIRED_AFTER_MATCH");
    }

    // ---------------------------------------------------------------- 5
    // 同时间事件次序：A@100, C@100, B@100 按此到达序喂入（批内顺序即到达序）。
    // 全序 (ts, seq)：A < C < B。C 打断同刻但 seq 更小的 A，故无匹配。
    private void case5SameTimestampOrder() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(
                e("A1", "A", 100), e("C2", "C", 100), e("B3", "B", 100)));
        eq(r.matches().size(), 0, "case5: 同刻 A C B 中 C 打断 A");
        eq(r.removed().get(0).reason().name(), "INTERRUPTED_BY_C", "case5: 打断原因正确");
    }

    // 同刻但顺序为 A, B（没有 C）：合法匹配。
    private void case5bSameTimestampAB() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(e("A1", "A", 100), e("B2", "B", 100)));
        eq(pairs(r.matches()), List.of(pair("k", "A1", "B2")),
                "case5b: tA==tB 且 A 先到，匹配成立");
    }

    // 同刻 B 先于 A 到达：B 到达时尚无 A，之后 A 等待——无匹配。
    private void case5cSameTimestampBFirst() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(e("B1", "B", 100), e("A2", "A", 100)));
        eq(r.matches().size(), 0, "case5c: 同刻 B 先到不匹配后到的 A");
    }

    // ---------------------------------------------------------------- 6
    // 边界：B 恰好在 tA+W（闭区间右端）仍匹配；tA+W+1 不匹配。
    private void case6Boundary() {
        StreamMatcher m1 = newEngine();
        EngineResult r1 = m1.process(List.of(e("A1", "A", 0), e("B2", "B", 100)));
        eq(r1.matches().size(), 1, "case6: tB - tA == W 闭区间匹配");

        StreamMatcher m2 = newEngine();
        EngineResult r2 = m2.process(List.of(e("A1", "A", 0), e("B2", "B", 101)));
        eq(r2.matches().size(), 0, "case6: tB - tA == W+1 不匹配");
        eq(r2.removed().get(0).reason().name(), "TIMEOUT", "case6: 超时");
    }

    // ---------------------------------------------------------------- 7
    // 乱序与迟到（L=0）：A1@0, B2@50（匹配）；随后迟到事件 A3@10 到达——
    // watermark 已为 50，10 < 50 被判迟到丢弃。之后 B4@60 只会匹配 A1，
    // 不会与被丢弃的 A3 匹配。
    private void case7OutOfOrderLate() {
        StreamMatcher m = newEngine();
        m.process(List.of(e("A1", "A", 0), e("B2", "B", 50)));
        EngineResult late = m.process(List.of(e("A3", "A", 10)));
        eq(late.lateDropped(), List.of("A3"), "case7: A3@10 迟到被 DROP");
        EngineResult after = m.process(List.of(e("B4", "B", 60)));
        eq(pairs(after.matches()), List.of(pair("k", "A1", "B4")),
                "case7: 迟到 A3 不参与后续匹配");
    }

    // ---------------------------------------------------------------- 8
    // 重放：乱序喂入的同一事件集，按 (timestamp, 到达序) 排序重算，A3 不再迟到：
    // 排序后 A1@0, A3@10, B2@50, B4@60 -> 4 个匹配（ALL 模式）。
    private void case8ReplaySorted() {
        StreamMatcher live = newEngine();
        live.process(List.of(e("A1", "A", 0), e("B2", "B", 50)));
        live.process(List.of(e("A3", "A", 10))); // 迟到
        live.process(List.of(e("B4", "B", 60)));
        // live 匹配数（去重 emitIndex 计数）
        eq(live.matches().size(), 2, "case8: 在线结果 2 个匹配");

        streammatch.reference.NaiveReferenceMatcher.ReferenceResult ref =
                streammatch.reference.NaiveReferenceMatcher.compute(
                        List.of(e("A1", "A", 0), e("A3", "A", 10),
                                e("B2", "B", 50), e("B4", "B", 60)),
                        W, MatchPolicy.ALL_CANDIDATES);
        eq(ref.matches().size(), 4, "case8: 理想重放为 4 个匹配");
        List<String> want = List.of(
                pair("k", "A1", "B2"), pair("k", "A3", "B2"),
                pair("k", "A1", "B4"), pair("k", "A3", "B4"));
        List<String> got = new ArrayList<>();
        ref.matches().forEach(rm -> got.add(pair(rm.key(), rm.aId(), rm.bId())));
        eq(got, want, "case8: 参考实现匹配对集合与手算一致");
    }

    // ---------------------------------------------------------------- 9
    // REJECT 策略：迟到事件导致整批拒绝且状态不变。
    private void case9RejectLate() {
        StreamMatcher m = StreamMatcher.eventTime(
                new EngineConfig(EngineMode.EVENT_TIME, W, MatchPolicy.ALL_CANDIDATES, 0L,
                        LatePolicy.REJECT));
        m.process(List.of(e("A1", "A", 0), e("X", "B", 80))); // watermark=80
        boolean threw = false;
        try {
            m.process(List.of(e("A2", "A", 5), e("A3", "A", 90)));
        } catch (StreamMatcher.LateEventException ex) {
            threw = true;
            eq(ex.lateIds(), List.of("A2"), "case9: 仅 A2 迟到并被报告");
        }
        check(threw, "case9: REJECT 下迟到抛异常");
        // 同批中的 A3@90 也必须未生效：active 中不应出现 A3，再来 B@95 只能匹配 A1
        EngineResult tail = m.process(List.of(e("B4", "B", 95)));
        eq(pairs(tail.matches()), List.of(pair("k", "A1", "B4")),
                "case9: 原子拒绝，A3 未进入状态");
    }

    // ---------------------------------------------------------------- 10
    // 多 key 互不影响：k1 的 A 只等 k1 的 B；k2 的 C 不打断 k1 的 A。
    private void case10KeysIndependent() {
        StreamMatcher m = newEngine();
        EngineResult r = m.process(List.of(
                new Event("A1", "k1", "A", 0, 0),
                new Event("C2", "k2", "C", 10, 0),
                new Event("B3", "k1", "B", 20, 0)));
        eq(pairs(r.matches()), List.of(pair("k1", "A1", "B3")),
                "case10: k2 的 C 不影响 k1");
    }
}

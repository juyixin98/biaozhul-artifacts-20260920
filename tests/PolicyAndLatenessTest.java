package tests;

import streammatch.engine.StreamMatcher;
import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.EngineResult;
import streammatch.model.Event;
import streammatch.model.LatePolicy;
import streammatch.model.Match;
import streammatch.model.MatchPolicy;

import java.util.ArrayList;
import java.util.List;

/** SKIP_PAST_LAST 重叠策略 与 allowedLateness 迟到窗口的语义测试。 */
public class PolicyAndLatenessTest extends TestCase {

    private static Event e(String id, String type, long ts) {
        return new Event(id, "k", type, ts, 0L);
    }

    private static List<String> pairs(List<Match> ms) {
        List<String> out = new ArrayList<>();
        for (Match m : ms) {
            out.add(pair(m.key(), m.aId(), m.bId()));
        }
        return out;
    }

    private static StreamMatcher skipEngine() {
        return StreamMatcher.eventTime(new EngineConfig(
                EngineMode.EVENT_TIME, 100, MatchPolicy.SKIP_PAST_LAST, 0L, LatePolicy.DROP));
    }

    private static StreamMatcher lateEngine(long lateness, LatePolicy policy) {
        return StreamMatcher.eventTime(new EngineConfig(
                EngineMode.EVENT_TIME, 100, MatchPolicy.ALL_CANDIDATES, lateness, policy));
    }

    @Override
    protected void run() {
        skipConsumesAs();
        skipMultipleBs();
        skipEarliestChosen();
        skipClearsOnC();
        latenessAccepted();
        latenessThenDropped();
    }

    // SKIP：A1@0,A2@10,B3@50 -> 只产出 (A1,B3)，A2 被 SKIPPED_AFTER_MATCH 清掉
    private void skipConsumesAs() {
        StreamMatcher m = skipEngine();
        EngineResult r = m.process(List.of(e("A1", "A", 0), e("A2", "A", 10), e("B3", "B", 50)));
        eq(pairs(r.matches()), List.of(pair("k", "A1", "B3")), "SKIP: 只匹配最早 A");
        eq(r.removed().size(), 1, "SKIP: 其余 A 被清理一个");
        eq(r.removed().get(0).aId(), "A2", "SKIP: 被清理的是 A2");
        eq(r.removed().get(0).reason().name(), "SKIPPED_AFTER_MATCH", "SKIP: 原因正确");
        // 后续 B 不再匹配已消费的 A1
        EngineResult tail = m.process(List.of(e("B4", "B", 80)));
        eq(tail.matches().size(), 0, "SKIP: A1 已消费，B4 无匹配");
    }

    // SKIP：匹配完成后新的 A 可以开启新一轮
    private void skipMultipleBs() {
        StreamMatcher m = skipEngine();
        m.process(List.of(e("A1", "A", 0), e("B2", "B", 30)));
        EngineResult r = m.process(List.of(e("A3", "A", 40), e("B4", "B", 60)));
        eq(pairs(r.matches()), List.of(pair("k", "A3", "B4")), "SKIP: 新一轮匹配");
    }

    // SKIP：最早 A 已超窗、第二个在窗内 -> B 选第二个
    private void skipEarliestChosen() {
        StreamMatcher m = skipEngine();
        EngineResult r = m.process(List.of(
                e("A1", "A", 0), e("A2", "A", 80), e("B3", "B", 120)));
        // watermark 到 120，A1（deadline=100）先超时；B3 与 A2（80..120 差40）匹配
        eq(pairs(r.matches()), List.of(pair("k", "A2", "B3")), "SKIP: 跳过已超时 A，选窗内最早 A");
        check(r.removed().stream().anyMatch(x -> x.aId().equals("A1")
                && x.reason().name().equals("TIMEOUT")), "SKIP: A1 先记超时");
    }

    // SKIP 下 C 清空等待，新 A 重新开始
    private void skipClearsOnC() {
        StreamMatcher m = skipEngine();
        EngineResult r = m.process(List.of(
                e("A1", "A", 0), e("A2", "A", 10), e("C3", "C", 20),
                e("A4", "A", 30), e("B5", "B", 50)));
        eq(pairs(r.matches()), List.of(pair("k", "A4", "B5")), "SKIP: C 后新一轮匹配");
    }

    // L=50：A1@0；最大时间到 100（wm=50）；随后事件 @60 仍算准时（>=50）
    private void latenessAccepted() {
        StreamMatcher m = lateEngine(50, LatePolicy.DROP);
        m.process(List.of(e("X", "B", 100))); // wm=50，无等待 A
        EngineResult r = m.process(List.of(e("A1", "A", 60), e("B2", "B", 90)));
        eq(r.lateDropped().size(), 0, "LATE: @60 在 watermark=50 之上，接受");
        eq(pairs(r.matches()), List.of(pair("k", "A1", "B2")), "LATE: 迟到窗口内事件参与匹配");
    }

    // L=50：wm=50；事件 @49 迟到丢弃；@50 恰好等于 watermark 不算迟到
    private void latenessThenDropped() {
        StreamMatcher m = lateEngine(50, LatePolicy.DROP);
        m.process(List.of(e("X", "B", 100))); // wm=50
        EngineResult r = m.process(List.of(
                e("LATE", "A", 49), e("EDGE", "A", 50)));
        eq(r.lateDropped(), List.of("LATE"), "LATE: 严格小于 watermark 才丢弃");
        eq(r.activeAKeys(), List.of("EDGE"), "LATE: 临界事件保留");
    }
}

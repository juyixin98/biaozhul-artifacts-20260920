package tests;

import streammatch.engine.StreamMatcher;
import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.Event;
import streammatch.model.LatePolicy;
import streammatch.model.Match;
import streammatch.model.MatchPolicy;
import streammatch.model.RemovedA;
import streammatch.reference.NaiveReferenceMatcher;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;
import java.util.TreeSet;

/**
 * 差分测试（fuzz）：随机生成“已按全序排列”的事件集，分别喂给流式引擎与暴力参考实现，
 * 要求匹配对集合完全一致。ALL_CANDIDATES 与 SKIP_PAST_LAST 两种策略都覆盖。
 *
 * <p>关键不变式：当事件按 {@code (timestamp, seq)} 顺序到达且 L=0（无迟到）时，
 * 流式引擎的输出必须与离线集合定义逐对相等——这独立验证了队列/超时/打断状态机。
 */
public class DifferentialTest extends TestCase {

    @Override
    protected void run() {
        Random rnd = new Random(20260923L);
        int scenarios = 0;
        for (int iter = 0; iter < 400; iter++) {
            for (MatchPolicy policy : MatchPolicy.values()) {
                scenarios++;
                runOne(rnd, policy, 1000L + rnd.nextInt(20), 1 + rnd.nextInt(40));
            }
        }
        eq(scenarios, 800, "差分场景数应为 800");
    }

    private void runOne(Random rnd, MatchPolicy policy, long window, int n) {
        // 时间戳生成：允许相等（同刻 tie-break）与少量“跳变”，覆盖窗口边界两侧
        List<Event> events = new ArrayList<>();
        long t = rnd.nextInt(5);
        int keyCount = 1 + rnd.nextInt(3);
        for (int i = 0; i < n; i++) {
            t += rnd.nextInt(4) == 0 ? rnd.nextInt(60) : rnd.nextInt(8);
            String key = "k" + rnd.nextInt(keyCount);
            int pick = rnd.nextInt(10);
            String type = pick < 4 ? "A" : pick < 8 ? "B" : "C"; // A/B 各 40%，C 20%
            events.add(new Event("e" + i, key, type, t, i));
        }
        // 已按 (timestamp, seq) 有序（生成即非降；相等时 i 升）
        events.sort(Comparator.comparingLong(Event::timestamp).thenComparingLong(Event::seq));
        for (int i = 0; i < events.size(); i++) {
            events.set(i, new Event(events.get(i).id(), events.get(i).key(),
                    events.get(i).type(), events.get(i).timestamp(), i));
        }

        // 流式引擎：逐事件喂（等价于一次性有序批，但逐事件更严格地检验中间状态）
        EngineConfig cfg = new EngineConfig(EngineMode.EVENT_TIME, window, policy, 0L, LatePolicy.DROP);
        StreamMatcher engine = StreamMatcher.eventTime(cfg);
        List<RemovedA> streamRemoved = new ArrayList<>();
        for (Event e : events) {
            streamRemoved.addAll(engine.process(List.of(e)).removed());
        }
        Set<String> streamPairs = toPairs(engine.matches());
        Set<String> streamRemovalKeys = removalKeys(streamRemoved);

        // 参考实现
        NaiveReferenceMatcher.ReferenceResult ref =
                NaiveReferenceMatcher.compute(events, window, policy);
        Set<String> refPairs = new HashSet<>();
        ref.matches().forEach(rm -> refPairs.add(pair(rm.key(), rm.aId(), rm.bId())));
        Set<String> refRemovalKeys = new TreeSet<>();
        ref.removed().forEach(rr -> refRemovalKeys.add(
                rr.key() + "|" + rr.aId() + "|" + rr.reason() + "|" + String.valueOf(rr.cId())));

        String ctx = "policy=" + policy + " window=" + window
                + " sequence=" + describe(events);
        if (!streamPairs.equals(refPairs)) {
            Set<String> onlyStream = new HashSet<>(streamPairs);
            onlyStream.removeAll(refPairs);
            Set<String> onlyRef = new HashSet<>(refPairs);
            onlyRef.removeAll(streamPairs);
            fail("匹配对不一致 " + ctx + " onlyStream=" + onlyStream
                    + " onlyReference=" + onlyRef);
        }
        // EXPIRED_AFTER_MATCH 在两边都只影响统计；这里按“是否曾匹配”对齐后比较结局集合
        if (!normalizeRemovals(streamRemovalKeys).equals(normalizeRemovals(refRemovalKeys))) {
            // 允许统计层面存在 EXPIRED_AFTER_MATCH 细节差异，但 TIMEOUT / INTERRUPTED / SKIPPED
            // 三类“未成功匹配即离场”的结局必须一致
            Set<String> sFatal = fatalRemovals(streamRemovalKeys);
            Set<String> rFatal = fatalRemovals(refRemovalKeys);
            if (!sFatal.equals(rFatal)) {
                fail("移除结局不一致 " + ctx
                        + " onlyStream=" + minus(sFatal, rFatal)
                        + " onlyReference=" + minus(rFatal, sFatal));
            }
        }
    }

    /** 结局集合中去掉 EXPIRED_AFTER_MATCH（统计项），只保留“未匹配成功”的离场原因。 */
    private static Set<String> fatalRemovals(Set<String> keys) {
        Set<String> out = new TreeSet<>();
        for (String k : keys) {
            if (!k.contains("|EXPIRED_AFTER_MATCH|")) {
                out.add(k);
            }
        }
        return out;
    }

    private static Set<String> normalizeRemovals(Set<String> keys) {
        return new TreeSet<>(keys);
    }

    private static Set<String> minus(Set<String> a, Set<String> b) {
        Set<String> r = new TreeSet<>(a);
        r.removeAll(b);
        return r;
    }

    private static Set<String> removalKeys(List<RemovedA> removed) {
        Set<String> s = new TreeSet<>();
        for (RemovedA r : removed) {
            s.add(r.key() + "|" + r.aId() + "|" + r.reason().name() + "|" + String.valueOf(r.cId()));
        }
        return s;
    }

    private static Set<String> toPairs(List<Match> ms) {
        Set<String> s = new HashSet<>();
        for (Match m : ms) {
            s.add(pair(m.key(), m.aId(), m.bId()));
        }
        return s;
    }

    private static String describe(List<Event> events) {
        StringBuilder sb = new StringBuilder();
        for (Event e : events) {
            sb.append(e.id()).append(':').append(e.key()).append('/')
                    .append(e.type()).append('@').append(e.timestamp()).append(' ');
        }
        return sb.toString().trim();
    }
}

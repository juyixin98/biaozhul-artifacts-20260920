package cep.pattern;

import cep.config.LatePolicy;
import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.KeyEvent;
import cep.model.Match;
import cep.model.Timeout;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 小数据精确参考实现（独立于 {@link PatternEngine} 的算法）。
 *
 * <p>假设事件按<b>已排好序</b>的方式一次性给入（等价于乱序容忍度为 0、无迟到），
 * 用最直白的扫描 + O(n²) 候选枚举得出结果，作为流式增量引擎的正确性基准：
 * <ol>
 *   <li>事件按全序 (ts, seq) 排序后逐个处理；</li>
 *   <li>处理每个事件前，先让 deadline &lt; 当前事件时间 的活跃 A 超时
 *       （半开区间，因此 deadline == 当前事件时间 不超时，窗口边界包含）；
 *      超时按 (deadline, A 全序) 发射；</li>
 *   <li>C 杀掉全序严格在它之前的活跃 A；B 在窗口内枚举候选 A，按策略匹配；</li>
 *   <li>结尾 flush，剩余活跃 A 全部超时。</li>
 * </ol>
 *
 * <p>注意第 2 步与流式引擎"事件/定时器统一归并、同刻事件优先"完全等价。
 */
public final class BruteForceMatcher {

    private static final class ActiveA {
        final KeyEvent event;
        final long deadline;
        boolean done;

        ActiveA(KeyEvent event, long deadline) {
            this.event = event;
            this.deadline = deadline;
        }
    }

    private final PatternConfig cfg;
    private final List<ActiveA> active = new ArrayList<>();
    private final List<Match> matches = new ArrayList<>();
    private final List<Timeout> timeouts = new ArrayList<>();
    private long matchCount = 0;
    private long timeoutCount = 0;
    private long killedCount = 0;

    private BruteForceMatcher(PatternConfig cfg) {
        this.cfg = cfg;
    }

    /**
     * 计算精确结果。事件会被复制后排序，调用方的 seq 约定与流式引擎一致
     * （未给 seq 时应在构造事件时按到达顺序补好）。
     */
    public static ReferenceResult evaluate(PatternConfig cfg, List<KeyEvent> events) {
        BruteForceMatcher m = new BruteForceMatcher(cfg);
        List<KeyEvent> sorted = new ArrayList<>(events);
        sorted.sort(Comparator.naturalOrder());

        for (KeyEvent e : sorted) {
            m.expireBefore(e.timestamp());
            m.process(e);
        }
        m.expireAll();
        return new ReferenceResult(m.matches, m.timeouts, m.matchCount,
                m.timeoutCount, m.killedCount, sorted.size());
    }

    private void expireBefore(long currentEventTime) {
        List<ActiveA> due = new ArrayList<>();
        for (ActiveA a : active) {
            if (!a.done && a.deadline < currentEventTime) {
                due.add(a);
            }
        }
        due.sort(Comparator.comparingLong((ActiveA a) -> a.deadline)
                .thenComparing(a -> a.event));
        for (ActiveA a : due) {
            timeout(a);
        }
        active.removeIf(a -> a.done);
    }

    private void expireAll() {
        List<ActiveA> rest = new ArrayList<>();
        for (ActiveA a : active) {
            if (!a.done) {
                rest.add(a);
            }
        }
        rest.sort(Comparator.comparingLong((ActiveA a) -> a.deadline)
                .thenComparing(a -> a.event));
        for (ActiveA a : rest) {
            timeout(a);
        }
        active.clear();
    }

    private void process(KeyEvent e) {
        if (cfg.aKey().equals(e.key())) {
            active.add(new ActiveA(e, saturatedAdd(e.timestamp(), cfg.windowMs())));
        } else if (cfg.cKey().equals(e.key())) {
            for (ActiveA a : active) {
                if (!a.done && a.event.compareTo(e) < 0) {
                    a.done = true;
                    killedCount++;
                }
            }
            active.removeIf(a -> a.done);
        } else if (cfg.bKey().equals(e.key())) {
            List<ActiveA> candidates = new ArrayList<>();
            for (ActiveA a : active) {
                if (!a.done
                        && a.event.compareTo(e) < 0
                        && e.timestamp() - a.event.timestamp() <= cfg.windowMs()) {
                    candidates.add(a);
                }
            }
            if (candidates.isEmpty()) {
                return;
            }
            List<ActiveA> chosen = switch (cfg.policy()) {
                case ALL_PAIRS -> new ArrayList<>(candidates);
                case EARLIEST_A, NON_OVERLAPPING -> List.of(candidates.get(0));
                case LATEST_A -> List.of(candidates.get(candidates.size() - 1));
            };
            for (ActiveA a : chosen) {
                a.done = true;
                matchCount++;
                matches.add(new Match(a.event.id(), e.id(), a.event.timestamp(),
                        e.timestamp(), e.timestamp(), false));
            }
            if (cfg.policy() == MatchPolicy.NON_OVERLAPPING) {
                for (ActiveA a : active) {
                    if (!a.done && a.event.compareTo(e) < 0) {
                        a.done = true; // 被贪婪区间消费，不产生超时
                    }
                }
            }
            active.removeIf(a -> a.done);
        }
        // latePolicy 不影响已排序输入（没有迟到），参数仅为配置完整性而保留
        if (cfg.latePolicy() == LatePolicy.DROP) {
            // no-op，仅表明参考实现对应 DROP 基线
        }
    }

    private void timeout(ActiveA a) {
        a.done = true;
        timeoutCount++;
        if (cfg.emitTimeouts()) {
            timeouts.add(new Timeout(a.event.id(), a.event.timestamp(),
                    a.deadline, false));
        }
    }

    private static long saturatedAdd(long a, long b) {
        long r = a + b;
        return b > 0 && r < a ? Long.MAX_VALUE : r;
    }
}

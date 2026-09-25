package cep.pattern;

import cep.config.LatePolicy;
import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.model.EngineStats;
import cep.model.KeyEvent;
import cep.model.Match;
import cep.model.Timeout;
import cep.time.Clock;
import cep.time.HeapScheduler;
import cep.time.ScheduledTask;
import cep.time.Scheduler;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.PriorityQueue;
import java.util.Set;

/**
 * 流式 "A 之后出现 B，期间无 C" 模式引擎（增量、有状态、事件时间驱动）。
 *
 * <h3>时间与确定性</h3>
 * <ul>
 *   <li>事件时间 = {@link KeyEvent#timestamp()}；同刻次序 = {@link KeyEvent#seq()}，
 *       合起来是全局全序 {@code (ts, seq)}；</li>
 *   <li>未显式给 seq 的事件按<b>到达顺序</b>补号；</li>
 *   <li>{@link Clock}/{@link Scheduler} 均可注入。引擎内部用调度器最小堆管理
 *       "A 的窗口到期定时器"，并把缓冲事件与到期定时器放在同一条事件时间轴上归并：
 *       二者同一时刻时<b>先处理事件、再触发定时器</b>（由此保证窗口边界包含，
 *       即 B 恰好在 A.ts + windowMs 到达仍算匹配）；</li>
 *   <li>自动水位 {@code wm = maxObservedEventTime - outOfOrderBound}（饱和减法），
 *       也可用 {@link #advanceWatermark(long)} 手动注入（只增不减，取手动/自动较大者）。</li>
 * </ul>
 *
 * <h3>迟到</h3>
 * 事件满足 {@code ts + allowedLateness < wm} 判为迟到：
 * {@code DROP} 直接丢弃计数；{@code ACCEPT} 在尽力车道按到达顺序立即处理，
 * 可以产生新匹配（标记 late），但<b>已发出的匹配/超时绝不撤回</b>。
 *
 * <p>结果（matches/timeouts）只增不改，顺序即发射顺序；同一 B 的多个匹配按
 * A 的全序（ts,seq）排列。
 */
public final class PatternEngine {

    private static final class ActiveA {
        final KeyEvent event;
        final long deadline;
        ScheduledTask timer;
        boolean done; // 已匹配/被消费/被杀/已超时

        ActiveA(KeyEvent event, long deadline) {
            this.event = event;
            this.deadline = deadline;
        }
    }

    private record OrderKey(long ts, long seq) implements Comparable<OrderKey> {
        @Override public int compareTo(OrderKey o) {
            int c = Long.compare(ts, o.ts);
            return c != 0 ? c : Long.compare(seq, o.seq);
        }
    }

    private final PatternConfig cfg;
    private final Scheduler scheduler;

    private final PriorityQueue<KeyEvent> pending = new PriorityQueue<>();
    /** 活跃 A，始终按全序 (ts, seq) 有序。 */
    private final List<ActiveA> active = new ArrayList<>();
    private final List<Match> matches = new ArrayList<>();
    private final List<Timeout> timeouts = new ArrayList<>();
    private final EngineStats stats = new EngineStats();
    private final Set<String> seenIds = new HashSet<>();
    private final Set<OrderKey> usedOrderKeys = new HashSet<>();

    private long arrivalCounter = 0L;
    private long maxObservedTime = Long.MIN_VALUE;
    private long explicitWatermark = Long.MIN_VALUE;
    private long watermark = Long.MIN_VALUE;
    private boolean finished = false;

    public PatternEngine(PatternConfig config) {
        this(config, new HeapScheduler(), Clock.system());
    }

    /**
     * @param config    模式配置
     * @param scheduler 事件时间调度器（推荐 {@link HeapScheduler}，reset 重放需要它支持 reset）
     * @param clock     时间来源（可注入虚拟时钟；匹配决策本身只依赖事件时间）
     */
    public PatternEngine(PatternConfig config, Scheduler scheduler, Clock clock) {
        this.cfg = config;
        this.scheduler = scheduler;
        if (clock == null) {
            throw new IllegalArgumentException("clock 不能为 null");
        }
    }

    // ------------------------------------------------------------- 输入

    /** 按到达顺序补 seq。重复 ID 会被忽略并计入 duplicates。 */
    public void ingest(String id, String key, long timestamp) {
        doIngest(id, key, timestamp, 0L, false);
    }

    /** 显式给出同刻次序 seq（同一 timestamp 内 seq 不得重复）。 */
    public void ingest(String id, String key, long timestamp, long seq) {
        doIngest(id, key, timestamp, seq, true);
    }

    private void doIngest(String id, String key, long timestamp, long seq, boolean seqProvided) {
        stats.received++;
        if (!seenIds.add(id)) {
            stats.duplicates++;
            return;
        }
        long effectiveSeq = seqProvided ? seq : arrivalCounter;
        OrderKey orderKey = new OrderKey(timestamp, effectiveSeq);
        if (!usedOrderKeys.add(orderKey)) {
            throw new IllegalArgumentException(
                    "事件 (" + id + ") 的全序键 (timestamp=" + timestamp + ", seq="
                            + effectiveSeq + ") 与已有事件冲突");
        }
        arrivalCounter++;
        KeyEvent e = new KeyEvent(id, key, timestamp, effectiveSeq);

        boolean late = watermark != Long.MIN_VALUE
                && timestamp != Long.MAX_VALUE
                && saturatingAdd(timestamp, cfg.allowedLateness()) < watermark;
        if (late) {
            if (cfg.latePolicy() == LatePolicy.DROP) {
                stats.droppedLate++;
                return;
            }
            stats.acceptedLate++;
            processEvent(e.withLate(true));
            return;
        }

        pending.add(e);
        if (timestamp > maxObservedTime) {
            maxObservedTime = timestamp;
        }
        long autoWm = maxObservedTime == Long.MIN_VALUE
                ? Long.MIN_VALUE : saturatingSub(maxObservedTime, cfg.outOfOrderBound());
        long target = Math.max(watermark, Math.max(explicitWatermark, autoWm));
        if (target > watermark) {
            advanceWatermarkTo(target, false);
        }
    }

    /** 手动注入水位（单调递增）。 */
    public void advanceWatermark(long newWatermark) {
        if (newWatermark < watermark) {
            throw new IllegalArgumentException(
                    "watermark 不能回退: " + newWatermark + " < " + watermark);
        }
        if (newWatermark > explicitWatermark) {
            explicitWatermark = newWatermark;
        }
        long autoWm = maxObservedTime == Long.MIN_VALUE
                ? Long.MIN_VALUE : saturatingSub(maxObservedTime, cfg.outOfOrderBound());
        long target = Math.max(newWatermark, autoWm);
        if (target > watermark) {
            advanceWatermarkTo(target, false);
        }
    }

    /** 终局推进：输出全部剩余匹配与超时。之后引擎只允许 reset 再用。 */
    public EngineResult flush() {
        advanceWatermarkTo(Long.MAX_VALUE, true);
        finished = true;
        return snapshot();
    }

    /** 清空全部状态（会话重放复用同一引擎实例；需要底层调度器支持 reset）。 */
    public void reset() {
        for (ActiveA a : active) {
            if (a.timer != null) {
                a.timer.cancel();
            }
        }
        scheduler.reset();
        pending.clear();
        active.clear();
        matches.clear();
        timeouts.clear();
        seenIds.clear();
        usedOrderKeys.clear();
        resetStats();
        arrivalCounter = 0L;
        maxObservedTime = Long.MIN_VALUE;
        explicitWatermark = Long.MIN_VALUE;
        watermark = Long.MIN_VALUE;
        finished = false;
    }

    private void resetStats() {
        stats.received = 0;
        stats.duplicates = 0;
        stats.droppedLate = 0;
        stats.acceptedLate = 0;
        stats.processed = 0;
        stats.matches = 0;
        stats.timeouts = 0;
        stats.cKilled = 0;
        stats.watermarks = 0;
    }

    public EngineResult snapshot() {
        return new EngineResult(matches, timeouts, stats, watermark);
    }

    public long watermark() { return watermark; }

    public EngineStats stats() { return stats; }

    public boolean isFinished() { return finished; }

    // ------------------------------------------------------------- 核心

    /**
     * 把内部时间推进到 target：循环挑选"可处理的缓冲事件"与"到期定时器"中时间更早者；
     * 同一时刻事件优先（保证窗口边界包含）。
     *
     * @param finalFlush true 时（flush）最后排空所有定时器，包括 deadline = Long.MAX_VALUE。
     */
    private void advanceWatermarkTo(long target, boolean finalFlush) {
        stats.watermarks++;
        while (true) {
            KeyEvent nextEvent = pending.peek();
            boolean eventDue = nextEvent != null && nextEvent.timestamp() < target;
            Long timerDeadline = scheduler.peekDeadline();
            boolean timerDue = timerDeadline != null && timerDeadline < target;

            if (!eventDue && !timerDue) {
                break;
            }
            if (eventDue && (!timerDue || nextEvent.timestamp() <= timerDeadline)) {
                pending.poll();
                processEvent(nextEvent);
            } else {
                long d = timerDeadline;
                scheduler.setTime(d);
                Runnable cb = scheduler.pollDueInclusive(d);
                if (cb != null) {
                    cb.run();
                }
            }
        }

        if (finalFlush) {
            // 终局：剩余事件（ts == MAX）与全部定时器排空
            KeyEvent e;
            while ((e = pending.poll()) != null) {
                processEvent(e);
            }
            Runnable cb;
            while ((cb = scheduler.pollDueInclusive(Long.MAX_VALUE)) != null) {
                cb.run();
            }
        }

        scheduler.setTime(target == Long.MAX_VALUE ? Long.MAX_VALUE - 1 : target);
        watermark = target; // 对外终局水位为 Long.MAX_VALUE（"+∞"）
    }

    private void processEvent(KeyEvent e) {
        stats.processed++;
        if (cfg.aKey().equals(e.key())) {
            long deadline = saturatingAdd(e.timestamp(), cfg.windowMs());
            ActiveA a = new ActiveA(e, deadline);

            // 迟到车道：窗口已完全落在当前水位之前 → 不会再有合格 B，立即超时
            if (e.late() && deadline < watermark) {
                a.done = true;
                stats.timeouts++;
                if (cfg.emitTimeouts()) {
                    timeouts.add(new Timeout(e.id(), e.timestamp(), deadline, true));
                }
                return;
            }

            insertSorted(a);
            a.timer = scheduler.schedule(deadline, () -> onTimeout(a));
        } else if (cfg.cKey().equals(e.key())) {
            killActiveAsBefore(e);
        } else if (cfg.bKey().equals(e.key())) {
            matchB(e);
        }
        // 其它 key：不参与模式，仅占用事件时间（已计入 processed）
    }

    private void onTimeout(ActiveA a) {
        if (a.done) {
            return;
        }
        a.done = true;
        active.remove(a);
        stats.timeouts++;
        if (cfg.emitTimeouts()) {
            timeouts.add(new Timeout(a.event.id(), a.event.timestamp(),
                    a.deadline, a.event.late()));
        }
    }

    /** C 到达：杀掉全序严格在它之前的所有活跃 A（不检查窗口，不发超时）。 */
    private void killActiveAsBefore(KeyEvent c) {
        var it = active.iterator();
        while (it.hasNext()) {
            ActiveA a = it.next();
            if (a.done) {
                it.remove();
                continue;
            }
            if (a.event.compareTo(c) < 0) {
                a.done = true;
                if (a.timer != null) {
                    scheduler.cancel(a.timer);
                }
                stats.cKilled++;
                it.remove();
            } else {
                break; // active 有序，后面的都不早于 c
            }
        }
    }

    private void matchB(KeyEvent b) {
        List<ActiveA> candidates = new ArrayList<>();
        for (ActiveA a : active) {
            if (a.done) {
                continue;
            }
            if (a.event.compareTo(b) >= 0) {
                break; // B 必须严格在 A 之后（同刻按 seq）
            }
            if (b.timestamp() - a.event.timestamp() <= cfg.windowMs()) {
                candidates.add(a);
            }
        }
        if (candidates.isEmpty()) {
            return;
        }

        List<ActiveA> matched = switch (cfg.policy()) {
            case ALL_PAIRS -> new ArrayList<>(candidates);
            case EARLIEST_A -> List.of(candidates.get(0));
            case NON_OVERLAPPING -> List.of(candidates.get(0));
            case LATEST_A -> List.of(candidates.get(candidates.size() - 1));
        };

        for (ActiveA a : matched) {
            emitMatch(a, b);
        }

        if (cfg.policy() == MatchPolicy.NON_OVERLAPPING) {
            // 贪婪不重叠：消费区间 [A0 .. B] 内全部活跃 A（无论是否在窗口内），
            // 被消费者不产生匹配、也不再超时。
            ActiveA first = candidates.get(0);
            for (ActiveA a : active) {
                if (a == first || a.done) {
                    continue;
                }
                if (a.event.compareTo(b) < 0) {
                    a.done = true;
                    if (a.timer != null) {
                        scheduler.cancel(a.timer);
                    }
                }
            }
            active.removeIf(a -> a.done);
        }
    }

    private void emitMatch(ActiveA a, KeyEvent b) {
        a.done = true;
        if (a.timer != null) {
            scheduler.cancel(a.timer);
        }
        active.remove(a);
        stats.matches++;
        matches.add(new Match(a.event.id(), b.id(), a.event.timestamp(),
                b.timestamp(), watermark, b.late() || a.event.late()));
    }

    private void insertSorted(ActiveA a) {
        int lo = 0, hi = active.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (active.get(mid).event.compareTo(a.event) < 0) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        active.add(lo, a);
    }

    private static long saturatingAdd(long a, long b) {
        long r = a + b;
        if (b > 0 && r < a) {
            return Long.MAX_VALUE;
        }
        return r;
    }

    private static long saturatingSub(long a, long b) {
        long r = a - b;
        if (b > 0 && r > a) {
            return Long.MIN_VALUE;
        }
        return r;
    }
}

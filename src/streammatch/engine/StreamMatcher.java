package streammatch.engine;

import streammatch.model.EngineConfig;
import streammatch.model.EngineResult;
import streammatch.model.Event;
import streammatch.model.Match;
import streammatch.model.MatchPolicy;
import streammatch.model.RemovedA;
import streammatch.time.Clock;
import streammatch.time.TaskScheduler;

import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Comparator;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 双模式流式匹配器。所有公开方法加锁，可在 HTTP 请求线程与墙钟调度器线程间共享。
 *
 * <h3>等待中 A 的内部结构</h3>
 * 每个 key 维护一个按到达顺序排列的队列（队首最早）。事件时间模式下队列中可能存在
 * “事件时间晚于当前”的早到 A；它们不参与当前 B 的匹配，也不会被早于其事件时间的 C 打断。
 */
public final class StreamMatcher implements PatternMatcher {

    private static final class WaitingA {
        final Event event;
        final long orderTime;   // ET: = event.timestamp；PT: 到达时钟读数
        long deadline;    // ET: timestamp + W（watermark 严格大于它才超时）；PT: orderTime + W + 1（定时器时刻；重配时可变）
        long timerHandle;       // PT: 调度器句柄；ET: -1
        boolean matched;        // ALL_CANDIDATES: 是否至少参与过一次匹配

        WaitingA(Event event, long orderTime, long deadline, long timerHandle) {
            this.event = event;
            this.orderTime = orderTime;
            this.deadline = deadline;
            this.timerHandle = timerHandle;
        }
    }

    /** 迟到且策略为 REJECT 时抛出；批处理在任何状态变更前完成判定，故为原子拒绝。 */
    public static final class LateEventException extends RuntimeException {
        private final List<String> lateIds;

        public LateEventException(List<String> lateIds) {
            super("late events rejected: " + lateIds);
            this.lateIds = List.copyOf(lateIds);
        }

        public List<String> lateIds() {
            return lateIds;
        }
    }

    private final boolean eventTimeMode;

    private EngineConfig config;
    private final Clock clock;           // PT 必填；ET 可为 null
    private final TaskScheduler scheduler; // PT 必填；ET 可为 null

    /** key -> 等待中 A 队列（TreeMap 让快照输出按 key 有序）。 */
    private final Map<String, Deque<WaitingA>> waiting = new TreeMap<>();

    private final List<Match> allMatches = new ArrayList<>();
    private final List<RemovedA> asyncRemoved = new ArrayList<>();

    private long seqCounter = 0;
    private long emitCounter = 0;
    private long maxEventTs = Long.MIN_VALUE;
    private long watermark = Long.MIN_VALUE;

    private StreamMatcher(EngineConfig config, Clock clock, TaskScheduler scheduler) {
        this.config = config;
        this.clock = clock;
        this.scheduler = scheduler;
        this.eventTimeMode = config.mode() == streammatch.model.EngineMode.EVENT_TIME;
    }

    /** 事件时间模式：不需要时钟与调度器。 */
    public static StreamMatcher eventTime(EngineConfig config) {
        if (config.mode() != streammatch.model.EngineMode.EVENT_TIME) {
            throw new IllegalArgumentException("eventTime() requires EVENT_TIME config");
        }
        return new StreamMatcher(config, null, null);
    }

    /** 处理时间模式：时钟与调度器均可注入（生产墙钟 / 测试手动）。 */
    public static StreamMatcher processingTime(EngineConfig config, Clock clock, TaskScheduler scheduler) {
        if (config.mode() != streammatch.model.EngineMode.PROCESSING_TIME) {
            throw new IllegalArgumentException("processingTime() requires PROCESSING_TIME config");
        }
        if (clock == null || scheduler == null) {
            throw new IllegalArgumentException("processingTime() requires clock and scheduler");
        }
        return new StreamMatcher(config, clock, scheduler);
    }

    @Override
    public synchronized EngineConfig config() {
        return config;
    }

    /**
     * 运行期修改配置。{@code mode} 不可变；其余字段可改。
     * 处理时间模式下改窗口会重新登记所有存活定时器；事件时间模式下改 L/W 会立即重算 watermark 并清理。
     */
    public synchronized void reconfigure(EngineConfig next) {
        if (next.mode() != config.mode()) {
            throw new IllegalArgumentException(
                    "mode cannot change at runtime: " + config.mode() + " -> " + next.mode());
        }
        if (!eventTimeMode) {
            // 用新窗口重排定时器
            for (Deque<WaitingA> q : waiting.values()) {
                for (WaitingA a : q) {
                    scheduler.cancel(a.timerHandle);
                    a.deadline = deadlineForProcessingTime(a.orderTime, next.windowMillis());
                    long h = scheduler.scheduleAt(a.deadline, () -> timeout(a));
                    a.timerHandle = h;
                }
            }
        }
        this.config = next;
        if (eventTimeMode && maxEventTs != Long.MIN_VALUE) {
            watermark = Math.max(watermark, saturatingSub(maxEventTs, next.allowedLateness()));
            pruneExpired(watermark, new ArrayList<>());
        }
    }

    @Override
    public synchronized EngineResult process(List<Event> arrivals) {
        List<Event> planned = new ArrayList<>(arrivals.size());
        long projectedMax = maxEventTs;
        long projectedWm = watermark;
        List<String> lateDropped = new ArrayList<>();
        List<String> lateRejected = new ArrayList<>();

        // 第一遍：分配 seq 并在不修改状态的前提下判定迟到（批内 watermark 也会推进）
        for (Event raw : arrivals) {
            Event e = assignSeq(raw);
            planned.add(e);
            if (eventTimeMode) {
                boolean late = projectedWm != Long.MIN_VALUE && e.timestamp() < projectedWm;
                if (late) {
                    if (config.latePolicy() == streammatch.model.LatePolicy.REJECT) {
                        lateRejected.add(e.id());
                    } else {
                        lateDropped.add(e.id());
                    }
                } else {
                    if (e.timestamp() > projectedMax) {
                        projectedMax = e.timestamp();
                        projectedWm = saturatingSub(projectedMax, config.allowedLateness());
                    }
                }
            }
        }
        if (!lateRejected.isEmpty()) {
            throw new LateEventException(lateRejected);
        }

        List<Match> matchesOut = new ArrayList<>();
        List<RemovedA> removedOut = new ArrayList<>();

        for (int i = 0; i < planned.size(); i++) {
            Event e = planned.get(i);
            boolean dropped = lateDropped.contains(e.id());
            if (dropped) {
                continue; // DROP：不产生任何状态变化，也不推进 watermark
            }
            if (eventTimeMode) {
                processEventTime(e, matchesOut, removedOut);
            } else {
                processProcessingTime(e, matchesOut, removedOut);
            }
        }

        return new EngineResult(matchesOut, removedOut, lateDropped, watermark, snapshotActive());
    }

    private void processEventTime(Event e, List<Match> matchesOut, List<RemovedA> removedOut) {
        // 统一的单事件语义：先把 watermark 推进到本事件时间并清理超时 A，再处理事件本身。
        // 这样“恰好在窗口外才到达的 C”不能打断已超时的 A（结局记 TIMEOUT 而非 INTERRUPTED_BY_C），
        // 与离线参考实现的全序扫描完全一致。
        if (e.timestamp() > maxEventTs) {
            maxEventTs = e.timestamp();
            watermark = saturatingSub(maxEventTs, config.allowedLateness());
            pruneExpired(watermark, removedOut);
        }
        switch (e.type()) {
            case Event.A -> enqueueA(e, e.timestamp());
            case Event.C -> interruptByC(e, e.timestamp(), removedOut);
            case Event.B -> matchB(e, e.timestamp(), matchesOut, removedOut);
            default -> throw new AssertionError(e.type());
        }
    }

    private void processProcessingTime(Event e, List<Match> matchesOut, List<RemovedA> removedOut) {
        long now = clock.now();
        pruneProcessingTime(now, removedOut);
        // 处理时间模式忽略事件自带 timestamp，内部统一盖到达时间戳
        Event stamped = new Event(e.id(), e.key(), e.type(), now, e.seq());
        switch (stamped.type()) {
            case Event.A -> {
                long deadline = deadlineForProcessingTime(now, config.windowMillis());
                WaitingA a = new WaitingA(stamped, now, deadline, -1);
                long handle = scheduler.scheduleAt(deadline, () -> timeout(a));
                a.timerHandle = handle;
                enqueueWaiting(a);
            }
            case Event.C -> interruptByC(stamped, now, removedOut);
            case Event.B -> matchB(stamped, now, matchesOut, removedOut);
            default -> throw new AssertionError(stamped.type());
        }
    }

    private void enqueueA(Event e, long orderTime) {
        long deadline = saturatingAdd(orderTime, config.windowMillis());
        enqueueWaiting(new WaitingA(e, orderTime, deadline, -1));
    }

    private void enqueueWaiting(WaitingA a) {
        waiting.computeIfAbsent(a.event.key(), k -> new ArrayDeque<>()).addLast(a);
    }

    /**
     * C 打断：清理同 key 中“在全序上位于 C 之前”的等待 A。
     * 全序条件 (tA &lt; tC) 或 (tA == tC 且 seqA &lt; seqC)；等待中的 A 必然 seq 更小，
     * 故事件时间模式简化为 {@code orderTime <= tC}（早到的“未来 A” orderTime &gt; tC 不受影响）。
     */
    private void interruptByC(Event c, long cOrderTime, List<RemovedA> removedOut) {
        Deque<WaitingA> q = waiting.get(c.key());
        if (q == null) {
            return;
        }
        q.removeIf(a -> {
            boolean precedesC = a.orderTime <= cOrderTime;
            if (precedesC) {
                if (a.timerHandle != -1) {
                    scheduler.cancel(a.timerHandle);
                }
                removedOut.add(RemovedA.interruptedByC(c.key(), a.event.id(), c.id()));
            }
            return precedesC;
        });
    }

    /**
     * B 到达时的匹配。窗口：0 &lt;= tB - tA &lt;= W（两端闭）。ALL 模式对全部合格 A 逐个产出，
     * A 保留可继续匹配；SKIP 模式只取最早合格 A 产出一次，随后清空全部等待 A。
     */
    private void matchB(Event b, long bOrderTime, List<Match> matchesOut, List<RemovedA> removedOut) {
        Deque<WaitingA> q = waiting.get(b.key());
        if (q == null || q.isEmpty()) {
            return;
        }
        long w = config.windowMillis();

        if (config.matchPolicy() == MatchPolicy.ALL_CANDIDATES) {
            for (WaitingA a : new ArrayList<>(q)) {
                long delta = bOrderTime - a.orderTime;
                if (delta >= 0 && delta <= w) {
                    a.matched = true;
                    matchesOut.add(emit(b, a));
                }
            }
            return;
        }

        // SKIP_PAST_LAST：最早合格 A（队列即到达序）→ 产出一次 → 清空等待
        WaitingA chosen = null;
        for (WaitingA a : q) {
            long delta = bOrderTime - a.orderTime;
            if (delta >= 0 && delta <= w) {
                chosen = a;
                break;
            }
        }
        if (chosen == null) {
            return;
        }
        matchesOut.add(emit(b, chosen));
        List<WaitingA> all = new ArrayList<>(q);
        q.clear();
        for (WaitingA a : all) {
            if (a.timerHandle != -1) {
                scheduler.cancel(a.timerHandle);
            }
            if (a != chosen) {
                removedOut.add(RemovedA.skippedAfterMatch(b.key(), a.event.id()));
            }
        }
    }

    private Match emit(Event b, WaitingA a) {
        Match m = new Match(a.event.key(), a.event.id(), b.id(),
                a.orderTime, b.timestamp(), a.event.seq(), b.seq(), emitCounter++);
        allMatches.add(m);
        return m;
    }

    /** PT 模式定时器回调：可能在调度器线程执行。 */
    private void timeout(WaitingA a) {
        synchronized (this) {
            Deque<WaitingA> q = waiting.get(a.event.key());
            if (q == null || !q.remove(a)) {
                return; // 已被 C / SKIP / 重配清理
            }
            RemovedA r = a.matched
                    ? new RemovedA(a.event.key(), a.event.id(),
                           streammatch.model.RemovalReason.EXPIRED_AFTER_MATCH, null)
                    : RemovedA.timeout(a.event.key(), a.event.id());
            asyncRemoved.add(r);
        }
    }

    /** PT 模式事件到达时的防御性清理：时钟已越过窗口上界（now &gt;= tA+W+1）的 A 先超时。 */
    private void pruneProcessingTime(long now, List<RemovedA> removedOut) {
        for (Map.Entry<String, Deque<WaitingA>> entry : waiting.entrySet()) {
            entry.getValue().removeIf(a -> {
                boolean expired = now >= a.deadline;
                if (expired) {
                    if (a.timerHandle != -1) {
                        scheduler.cancel(a.timerHandle);
                    }
                    removedOut.add(a.matched
                            ? new RemovedA(entry.getKey(), a.event.id(),
                                    streammatch.model.RemovalReason.EXPIRED_AFTER_MATCH, null)
                            : RemovedA.timeout(entry.getKey(), a.event.id()));
                }
                return expired;
            });
        }
    }

    /** ET 模式：watermark 严格大于 tA+W 时超时。 */
    private void pruneExpired(long wm, List<RemovedA> removedOut) {
        if (wm == Long.MIN_VALUE) {
            return;
        }
        for (Map.Entry<String, Deque<WaitingA>> entry : waiting.entrySet()) {
            entry.getValue().removeIf(a -> {
                boolean expired = wm > a.deadline;
                if (expired) {
                    removedOut.add(a.matched
                            ? new RemovedA(entry.getKey(), a.event.id(),
                                    streammatch.model.RemovalReason.EXPIRED_AFTER_MATCH, null)
                            : RemovedA.timeout(entry.getKey(), a.event.id()));
                }
                return expired;
            });
        }
    }

    @Override
    public synchronized EngineResult advanceWatermark(long targetWatermark) {
        if (!eventTimeMode) {
            throw new UnsupportedOperationException("advanceWatermark is only available in EVENT_TIME mode");
        }
        List<Match> noMatches = new ArrayList<>();
        List<RemovedA> removed = new ArrayList<>();
        if (targetWatermark > watermark) {
            watermark = targetWatermark;
            pruneExpired(watermark, removed);
        }
        return new EngineResult(noMatches, removed, List.of(), watermark, snapshotActive());
    }

    /** 取出 PT 模式下由调度器线程异步产生的移除记录（破坏性读取）。 */
    public synchronized List<RemovedA> drainAsyncRemoved() {
        if (asyncRemoved.isEmpty()) {
            return List.of();
        }
        List<RemovedA> out = List.copyOf(asyncRemoved);
        asyncRemoved.clear();
        return out;
    }

    @Override
    public synchronized List<Match> matches() {
        return List.copyOf(allMatches);
    }

    @Override
    public synchronized List<String> activeAIds() {
        return snapshotActive();
    }

    @Override
    public synchronized long watermark() {
        return watermark;
    }

    /** 测试/重置用：清空全部状态（配置保留）。 */
    public synchronized void reset() {
        for (Deque<WaitingA> q : waiting.values()) q.clear();
        waiting.clear();
        allMatches.clear();
        asyncRemoved.clear();
        seqCounter = 0;
        emitCounter = 0;
        maxEventTs = Long.MIN_VALUE;
        watermark = Long.MIN_VALUE;
    }

    private List<String> snapshotActive() {
        record Row(String id, String key, long t, long seq) {
        }
        List<Row> rows = new ArrayList<>();
        for (Map.Entry<String, Deque<WaitingA>> e : waiting.entrySet()) {
            for (WaitingA a : e.getValue()) {
                rows.add(new Row(a.event.id(), e.getKey(), a.orderTime, a.event.seq()));
            }
        }
        rows.sort(Comparator.comparing((Row r) -> r.key).thenComparingLong(r -> r.t)
                .thenComparingLong(r -> r.seq));
        return rows.stream().map(Row::id).toList();
    }

    private Event assignSeq(Event raw) {
        return new Event(raw.id(), raw.key(), raw.type(), raw.timestamp(), seqCounter++);
    }

    private static long deadlineForProcessingTime(long orderTime, long windowMillis) {
        // 闭窗口 [tA, tA+W]：定时器在 tA+W+1 触发；饱和加法避免溢出
        long end = saturatingAdd(orderTime, windowMillis);
        return end == Long.MAX_VALUE ? Long.MAX_VALUE : end + 1;
    }

    private static long saturatingAdd(long a, long b) {
        long r = a + b;
        if (b > 0 && r < a) {
            return Long.MAX_VALUE;
        }
        if (b < 0 && r > a) {
            return Long.MIN_VALUE;
        }
        return r;
    }

    private static long saturatingSub(long a, long b) {
        return saturatingAdd(a, -b);
    }
}

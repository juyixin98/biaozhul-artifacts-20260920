package com.example.watermark.watermarks;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

import com.example.watermark.time.Clock;
import com.example.watermark.time.ScheduledTask;
import com.example.watermark.time.Scheduler;

/**
 * Multi-partition watermark aggregator with idle detection.
 *
 * <h2>Global watermark</h2>
 * The global watermark is the minimum of the local watermarks of all
 * partitions currently feeding it ({@link PartitionState#ACTIVE}). Partitions
 * in {@link PartitionState#WAITING} (registered, no events yet) and
 * {@link PartitionState#IDLE} are excluded so a quiet partition cannot stall
 * progress. When no partition feeds the aggregate the global watermark simply
 * holds its last value (never resets).
 *
 * <h2>Monotonicity</h2>
 * Both local and global watermarks are clamped to never move backwards. In
 * particular, when a partition resumes after an idle pause its stale local
 * watermark cannot pull the global value down.
 *
 * <h2>Late events</h2>
 * An event with {@code timestamp <= globalWatermark} is diverted to the late
 * channel ({@link #addLateEventListener}) and never advances any watermark.
 * This is exactly what happens to old events replayed by a resumed partition:
 * the resumed partition's generator only starts seeing events from the first
 * event newer than the global watermark.
 *
 * <h2>Idleness</h2>
 * On every periodic tick an active partition whose last event arrived at least
 * {@code idleTimeoutMillis} ago (processing time from the injected
 * {@link Clock}) becomes idle; the first subsequent event reactivates it.
 * Idle detection granularity is therefore one emission interval.
 *
 * @param <T> event payload type
 */
public final class WatermarkManager<T> implements AutoCloseable {

    /** Per-partition mutable state. */
    private static final class Partition<TT> {
        final String key;
        final WatermarkGenerator<TT> generator;
        PartitionState state = PartitionState.WAITING;
        long localWatermark = Watermark.NO_WATERMARK;
        long lastEventTime = Long.MIN_VALUE;

        Partition(String key, WatermarkGenerator<TT> generator) {
            this.key = key;
            this.generator = generator;
        }
    }

    private final WatermarkStrategy<T> strategy;
    private final Clock clock;
    private final Scheduler scheduler;
    private final long idleTimeout;
    private final boolean idleDetectionEnabled;

    private final Map<String, Partition<T>> partitions = new LinkedHashMap<>();
    private final List<LateEvent> lateEvents = new ArrayList<>();

    private long globalWatermark = Watermark.NO_WATERMARK;
    private ScheduledTask tickTask;
    private volatile boolean running;

    private final CopyOnWriteArrayList<java.util.function.Consumer<Long>> globalWatermarkListeners =
            new CopyOnWriteArrayList<>();
    private final CopyOnWriteArrayList<java.util.function.Consumer<LateEvent>> lateEventListeners =
            new CopyOnWriteArrayList<>();
    private final CopyOnWriteArrayList<PartitionStateListener> partitionStateListeners =
            new CopyOnWriteArrayList<>();

    /** Notified when a partition changes state (WAITING/ACTIVE/IDLE). */
    public interface PartitionStateListener {
        void stateChanged(String partition, PartitionState oldState, PartitionState newState);
    }

    public WatermarkManager(WatermarkStrategy<T> strategy, Clock clock, Scheduler scheduler) {
        this.strategy = strategy;
        this.clock = clock;
        this.scheduler = scheduler;
        long idle = strategy.config().idleTimeoutMillis();
        this.idleTimeout = idle;
        this.idleDetectionEnabled = idle > 0;
    }

    /** Register a partition up front; it starts in WAITING and does not block the aggregate. */
    public synchronized void registerPartition(String key) {
        if (partitions.containsKey(key)) {
            return;
        }
        Partition<T> p = new Partition<>(key, strategy.generatorSupplier().get());
        partitions.put(key, p);
    }

    /** Start the periodic emission / idle-detection task. */
    public synchronized void start() {
        if (running) {
            return;
        }
        long interval = strategy.config().autoWatermarkIntervalMillis();
        tickTask = scheduler.schedulePeriodically(this::tick, interval, interval);
        running = true;
    }

    @Override
    public synchronized void close() {
        if (tickTask != null) {
            tickTask.cancel();
            tickTask = null;
        }
        running = false;
    }

    public boolean isRunning() {
        return running;
    }

    // ------------------------------------------------------------------
    // Event ingress
    // ------------------------------------------------------------------

    /**
     * Feed one event. Unknown partitions are auto-registered (starting in
     * WAITING), mirroring dynamic partition discovery.
     *
     * @return true if the event was on time, false if it went to the late channel
     */
    public boolean onEvent(StreamEvent event) {
        long now;
        Partition<T> p;
        boolean resumed;
        synchronized (this) {
            p = partitions.get(event.key());
            if (p == null) {
                registerPartition(event.key());
                p = partitions.get(event.key());
            }
            now = clock.currentTimeMillis();
            PartitionState oldState = p.state;
            resumed = oldState == PartitionState.IDLE;

            // Late event: event time already covered by the global watermark.
            if (globalWatermark != Watermark.NO_WATERMARK
                    && event.timestamp() <= globalWatermark) {
                if (oldState != PartitionState.ACTIVE) {
                    transition(p, PartitionState.ACTIVE, now);
                } else {
                    p.lastEventTime = now;
                }
                LateEvent late = new LateEvent(event, globalWatermark, now, resumed);
                lateEvents.add(late);
                dispatchLate(late);
                return false;
            }

            if (oldState != PartitionState.ACTIVE) {
                transition(p, PartitionState.ACTIVE, now);
            }
            p.lastEventTime = now;
            p.generator.onEvent(event, now);
        }
        return true;
    }

    // ------------------------------------------------------------------
    // Periodic work
    // ------------------------------------------------------------------

    /** Run one emission/idle tick manually (also what the scheduler calls). */
    public synchronized void tick() {
        long now = clock.currentTimeMillis();

        if (idleDetectionEnabled) {
            for (Partition<T> p : partitions.values()) {
                if (p.state == PartitionState.ACTIVE
                        && p.lastEventTime != Long.MIN_VALUE
                        && now - p.lastEventTime >= idleTimeout) {
                    transition(p, PartitionState.IDLE, now);
                }
            }
        }

        for (Partition<T> p : partitions.values()) {
            if (p.state == PartitionState.ACTIVE) {
                p.generator.onPeriodicEmit(w -> {
                    // Clamp local watermark: never regress, ignore NO_WATERMARK sentinel.
                    if (w != Watermark.NO_WATERMARK && w > p.localWatermark) {
                        p.localWatermark = w;
                    }
                }, now);
            }
        }

        recomputeGlobalWatermark();
    }

    /** Run a periodic emission immediately regardless of the scheduled time. */
    public void emitNow() {
        tick();
    }

    /** Advance-style hook for callers driving a virtual clock without a scheduler. */
    public synchronized void onProcessingTimeTick() {
        tick();
    }

    private void recomputeGlobalWatermark() {
        long min = Watermark.NO_WATERMARK;
        boolean any = false;
        for (Partition<T> p : partitions.values()) {
            if (p.state == PartitionState.ACTIVE && p.localWatermark != Watermark.NO_WATERMARK) {
                min = any ? Math.min(min, p.localWatermark) : p.localWatermark;
                any = true;
            }
        }
        if (!any) {
            // No partition can contribute: hold the previous global watermark.
            return;
        }
        // Global clamp: resumed partitions with stale local watermarks must not
        // pull the global watermark backwards.
        if (min > globalWatermark) {
            long old = globalWatermark;
            globalWatermark = min;
            long current = globalWatermark;
            for (var l : globalWatermarkListeners) {
                l.accept(current);
            }
        }
    }

    private void transition(Partition<T> p, PartitionState newState, long now) {
        PartitionState old = p.state;
        if (old == newState) {
            return;
        }
        p.state = newState;
        p.lastEventTime = now;
        for (PartitionStateListener l : partitionStateListeners) {
            l.stateChanged(p.key, old, newState);
        }
    }

    private void dispatchLate(LateEvent late) {
        for (var l : lateEventListeners) {
            l.accept(late);
        }
    }

    // ------------------------------------------------------------------
    // Listeners / accessors
    // ------------------------------------------------------------------

    public void addGlobalWatermarkListener(java.util.function.Consumer<Long> listener) {
        globalWatermarkListeners.add(listener);
    }

    public void addLateEventListener(java.util.function.Consumer<LateEvent> listener) {
        lateEventListeners.add(listener);
    }

    public void addPartitionStateListener(PartitionStateListener listener) {
        partitionStateListeners.add(listener);
    }

    /** Global watermark, or {@link Watermark#NO_WATERMARK}. */
    public synchronized long getGlobalWatermark() {
        return globalWatermark;
    }

    /** Local watermark of a partition, or {@link Watermark#NO_WATERMARK}. */
    public synchronized long getLocalWatermark(String partition) {
        Partition<T> p = partitions.get(partition);
        return p == null ? Watermark.NO_WATERMARK : p.localWatermark;
    }

    public synchronized PartitionState getPartitionState(String partition) {
        Partition<T> p = partitions.get(partition);
        return p == null ? null : p.state;
    }

    public synchronized List<String> getPartitionKeys() {
        return new ArrayList<>(partitions.keySet());
    }

    /** Snapshot of all late events seen so far (copy). */
    public synchronized List<LateEvent> getLateEvents() {
        return new ArrayList<>(lateEvents);
    }

    /** Immutable snapshot of manager state, suitable for JSON serialization. */
    public synchronized Snapshot snapshot() {
        Map<String, PartitionSnapshot> ps = new LinkedHashMap<>();
        for (Partition<T> p : partitions.values()) {
            ps.put(p.key, new PartitionSnapshot(
                    p.state.name(),
                    p.localWatermark == Watermark.NO_WATERMARK ? null : p.localWatermark,
                    p.lastEventTime == Long.MIN_VALUE ? null : p.lastEventTime));
        }
        return new Snapshot(
                globalWatermark == Watermark.NO_WATERMARK ? null : globalWatermark,
                clock.currentTimeMillis(),
                ps,
                lateEvents.size());
    }

    /** Manager-wide state snapshot. */
    public record Snapshot(
            Long globalWatermark,
            long processingTimeMillis,
            Map<String, PartitionSnapshot> partitions,
            int lateEventCount) {

        /** Plain-map view so generic JSON serializers (records unsupported) can emit it. */
        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("globalWatermark", globalWatermark);
            m.put("processingTimeMillis", processingTimeMillis);
            Map<String, Object> ps = new LinkedHashMap<>();
            partitions.forEach((k, v) -> ps.put(k, v.toMap()));
            m.put("partitions", ps);
            m.put("lateEventCount", lateEventCount);
            return m;
        }
    }

    /** Per-partition state snapshot. */
    public record PartitionSnapshot(
            String state,
            Long localWatermark,
            Long lastEventTime) {

        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("state", state);
            m.put("localWatermark", localWatermark);
            m.put("lastEventTime", lastEventTime);
            return m;
        }
    }
}

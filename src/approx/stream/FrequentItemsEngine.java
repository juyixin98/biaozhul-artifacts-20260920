package approx.stream;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import approx.cms.BoundedCandidateSet;
import approx.cms.CountMinSketch;
import approx.cms.ExactCounter;
import approx.cms.SketchIncompatibleException;
import approx.cms.SketchSnapshot;
import approx.time.Clock;
import approx.time.TaskScheduler;

/**
 * Event-stream engine: per-window Count-Min Sketch + bounded candidate set
 * (+ optional exact reference counter), with injectable time and scheduling.
 *
 * <p>Windows are tumbling and aligned to the engine creation time: window
 * {@code w} covers {@code [w0 + w*L, w0 + (w+1)*L)}. Rotation is driven either
 * by injected scheduler ticks (production) or directly by an out-of-window
 * event arriving early (so ordering of calls is irrelevant for tests). Events
 * whose timestamp precedes the active window start are counted as late and
 * dropped, never retroactively changing a closed window.
 */
public final class FrequentItemsEngine implements AutoCloseable {

    /** How many closed windows to retain for {@code GET /windows/{n}/result}. */
    static final int MAX_CLOSED_RESULTS = 64;

    private final String id;
    private final EngineConfig config;
    private final Clock clock;
    private final TaskScheduler scheduler;

    private final Deque<WindowResult> closed = new ArrayDeque<>();

    // Active-window mutable state (all guarded by this).
    private long windowStart;
    private CountMinSketch sketch;
    private BoundedCandidateSet candidates;
    private ExactCounter exact; // null unless trackExact
    private long acceptedCount;
    private long droppedLateCount;
    private boolean started;

    private TaskScheduler.CancellationToken scheduleHandle;

    public FrequentItemsEngine(String id, EngineConfig config, Clock clock, TaskScheduler scheduler) {
        this.id = id;
        this.config = config;
        this.clock = clock;
        this.scheduler = scheduler;
        this.windowStart = config.windowMillis == 0 ? 0L : clock.nowMillis();
        resetWindow();
    }

    public String id() {
        return id;
    }

    public EngineConfig config() {
        return config;
    }

    private void resetWindow() {
        this.sketch = new CountMinSketch(config.width, config.depth, config.seed);
        this.candidates = new BoundedCandidateSet(config.candidateCapacity, sketch);
        this.exact = config.trackExact ? new ExactCounter() : null;
    }

    /** Begin periodic rotation (no-op for a non-rotating engine). */
    public synchronized void start() {
        if (started) {
            return;
        }
        started = true;
        if (config.windowMillis > 0) {
            // Re-align the initial window to "now" when start time differs from construction.
            long now = clock.nowMillis();
            long elapsed = now - windowStart;
            windowStart = now - Math.floorDiv(elapsed, config.windowMillis) * config.windowMillis;
            long firstDelay = config.windowMillis - Math.floorMod(now - windowStart, config.windowMillis);
            scheduleHandle = scheduler.schedulePeriodic(
                    firstDelay, config.windowMillis, this::tick);
        }
    }

    /** Scheduler entry point: rotate every window that has fully elapsed. */
    public synchronized void tick() {
        if (config.windowMillis <= 0) {
            return;
        }
        long now = clock.nowMillis();
        while (now >= windowStart + config.windowMillis) {
            closeWindow(windowStart + config.windowMillis);
        }
    }

    private void closeWindow(long endBoundary) {
        List<Map.Entry<String, Long>> topK = candidates.topK(config.candidateCapacity);
        List<Map.Entry<String, Long>> exactTopK =
                exact == null ? null : exact.topK(config.candidateCapacity);
        WindowResult result = new WindowResult(
                windowStart, endBoundary, sketch.totalCount(), candidates.size(),
                topK, exactTopK, sketch.snapshot(), sketch.errorUpperBound());
        closed.addLast(result);
        while (closed.size() > MAX_CLOSED_RESULTS) {
            closed.removeFirst();
        }
        windowStart = endBoundary;
        resetWindow();
    }

    /** Result of an event-batch insertion. */
    public static final class AddResult {
        public final long accepted;
        public final long droppedLate;

        AddResult(long accepted, long droppedLate) {
            this.accepted = accepted;
            this.droppedLate = droppedLate;
        }
    }

    /**
     * Add an event to the window containing its timestamp.
     *
     * @return always {@code accepted=1} unless the event is late, in which case
     *     it is counted in {@code droppedLate}.
     */
    public synchronized AddResult addEvent(Event<String> event) {
        long ts = event.timestampMillis;
        if (config.windowMillis > 0) {
            if (ts < windowStart) {
                droppedLateCount++;
                return new AddResult(0, 1);
            }
            while (ts >= windowStart + config.windowMillis) {
                closeWindow(windowStart + config.windowMillis);
            }
        }
        sketch.add(event.item);
        candidates.observe(event.item);
        if (exact != null) {
            exact.add(event.item);
        }
        acceptedCount++;
        return new AddResult(1, 0);
    }

    /** Add a batch; returns aggregate accepted/late counters. */
    public synchronized AddResult addEvents(Iterable<Event<String>> events) {
        long accepted = 0;
        long late = 0;
        for (Event<String> e : events) {
            AddResult r = addEvent(e);
            accepted += r.accepted;
            late += r.droppedLate;
        }
        return new AddResult(accepted, late);
    }

    /**
     * Force the active window to close now. For rotating windows the next window
     * starts at the next boundary; for a non-rotating engine the next window
     * starts at the current injected time.
     */
    public synchronized WindowResult flush() {
        long end;
        if (config.windowMillis > 0) {
            long now = clock.nowMillis();
            end = windowStart + config.windowMillis;
            closeWindow(end);
            // If several boundaries have passed without ticks, catch up like tick().
            while (now >= windowStart + config.windowMillis) {
                closeWindow(windowStart + config.windowMillis);
            }
            return closed.peekLast();
        }
        end = clock.nowMillis();
        closeWindow(end);
        windowStart = end;
        return closed.peekLast();
    }

    /** Candidate top-K projection of the active window (coverage NOT guaranteed). */
    public synchronized List<Map.Entry<String, Long>> currentTopK(int k) {
        return candidates.topK(k);
    }

    /**
     * Point estimate for one item in the active window. The returned map carries
     * {@code estimate}, the exact {@code trueCount} (only when trackExact), and
     * the declared one-sided {@code errorUpperBound} with its confidence.
     */
    public synchronized Map<String, Object> queryItem(String item) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("item", item);
        m.put("estimate", sketch.estimate(item));
        if (exact != null) {
            m.put("trueCount", exact.countOf(item));
        }
        m.put("errorUpperBound", sketch.errorUpperBound());
        m.put("epsilon", sketch.epsilon());
        m.put("delta", sketch.delta());
        m.put("totalCount", sketch.totalCount());
        m.put("retainedCandidate", candidates.contains(item));
        return m;
    }

    /**
     * Merge a serialized sketch into the active window.
     *
     * @throws SketchIncompatibleException if width/depth/seed differ.
     */
    public synchronized void mergeSketch(SketchSnapshot other) {
        sketch.mergeSnapshot(other);
    }

    public synchronized SketchSnapshot activeSnapshot() {
        return sketch.snapshot();
    }

    public synchronized List<WindowResult> closedWindows() {
        return new ArrayList<>(closed);
    }

    /** Service/observability snapshot. */
    public synchronized Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("windowStartMillis", windowStart);
        m.put("windowMillis", config.windowMillis);
        m.put("width", config.width);
        m.put("depth", config.depth);
        m.put("seed", config.seed);
        m.put("candidateCapacity", config.candidateCapacity);
        m.put("trackExact", config.trackExact);
        m.put("acceptedTotal", acceptedCount);
        m.put("droppedLateTotal", droppedLateCount);
        m.put("activeWindowTotal", sketch.totalCount());
        m.put("activeCandidates", candidates.size());
        m.put("activeErrorUpperBound", sketch.errorUpperBound());
        m.put("closedWindows", closed.size());
        return m;
    }

    @Override
    public synchronized void close() {
        if (scheduleHandle != null) {
            scheduleHandle.cancel();
            scheduleHandle = null;
        }
    }
}

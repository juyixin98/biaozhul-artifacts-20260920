package approxheavy.stream;

import approxheavy.candidates.BoundedCandidates;
import approxheavy.cms.CountMinSketch;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;

/**
 * Ingests events into tumbling windows of {@code windowMillis} length.
 *
 * <p>Time and scheduling are injected ({@link approxheavy.core.Clock},
 * {@link approxheavy.core.Scheduler}), so the same code runs against the wall
 * clock in production and a manual clock/scheduler in deterministic tests.
 *
 * <p>Events arriving for a future window boundary materialize all skipped
 * windows, including empty ones. Late events (before the current window start)
 * are rejected via {@link #ingest}'s return value rather than silently dropped.
 *
 * <p>Sealed windows can be merged ({@link #mergeWindow}) to combine parallel
 * partitions: merge rejects sketches with mismatched width/depth/seed.
 */
public final class EventStreamProcessor {
    /** Result of an ingest attempt. */
    public enum IngestStatus { ACCEPTED, LATE }

    private final int width;
    private final int depth;
    private final long seed;
    private final int candidateCapacity;
    private final long windowMillis;
    private final int retainedWindows;
    private final approxheavy.core.Clock clock;
    private final approxheavy.core.Scheduler scheduler;

    private long windowStart;
    private CountMinSketch activeSketch;
    private BoundedCandidates activeCandidates;
    private final Deque<WindowSummary> sealed = new ArrayDeque<>();
    private long scheduledTaskId = -1L;

    public EventStreamProcessor(int width, int depth, long seed, int candidateCapacity,
                                long windowMillis, int retainedWindows,
                                approxheavy.core.Clock clock,
                                approxheavy.core.Scheduler scheduler) {
        if (windowMillis <= 0) {
            throw new IllegalArgumentException("windowMillis must be > 0");
        }
        if (retainedWindows < 1) {
            throw new IllegalArgumentException("retainedWindows must be >= 1");
        }
        this.width = width;
        this.depth = depth;
        this.seed = seed;
        this.candidateCapacity = candidateCapacity;
        this.windowMillis = windowMillis;
        this.retainedWindows = retainedWindows;
        this.clock = clock;
        this.scheduler = scheduler;
        this.windowStart = floorToWindow(clock.millis());
        openWindow(windowStart);
        if (scheduler != null) {
            this.scheduledTaskId = scheduler.scheduleAtFixedRate(
                    Math.max(1, windowMillis / 10), this::onTick);
        }
    }

    private void openWindow(long start) {
        this.activeSketch = new CountMinSketch(width, depth, seed);
        this.activeCandidates = new BoundedCandidates(candidateCapacity, activeSketch::estimate);
        this.windowStart = start;
    }

    private long floorToWindow(long millis) {
        return Math.floorDiv(millis, windowMillis) * windowMillis;
    }

    /** Seal the active window if the clock has crossed its boundary. */
    private synchronized void onTick() {
        long now = clock.millis();
        long currentStart = floorToWindow(now);
        while (windowStart < currentStart) {
            sealActive();
            openWindow(windowStart + windowMillis);
        }
    }

    private void sealActive() {
        WindowSummary summary = new WindowSummary(
                windowStart, windowStart + windowMillis,
                activeSketch, activeCandidates, candidateCapacity);
        sealed.addLast(summary);
        while (sealed.size() > retainedWindows) {
            sealed.removeFirst();
        }
    }

    /**
     * Ingest one event with an explicit event timestamp.
     *
     * @return ACCEPTED, or LATE if the timestamp precedes the active window
     */
    public synchronized IngestStatus ingest(String key, long timestampMillis) {
        return ingest(key, 1L, timestampMillis);
    }

    public synchronized IngestStatus ingest(String key, long count, long timestampMillis) {
        if (key == null || key.isEmpty()) {
            throw new IllegalArgumentException("key required");
        }
        if (count < 0) {
            throw new IllegalArgumentException("count must be non-negative");
        }
        long eventWindow = floorToWindow(timestampMillis);
        if (eventWindow < windowStart) {
            return IngestStatus.LATE;
        }
        while (windowStart < eventWindow) {
            sealActive();
            openWindow(windowStart + windowMillis);
        }
        activeSketch.add(key, count);
        activeCandidates.observe(key);
        return IngestStatus.ACCEPTED;
    }

    /** Ingest at the injected clock's current time. */
    public IngestStatus ingest(String key) {
        return ingest(key, 1L, clock.millis());
    }

    /** Force-seal the active window immediately (mainly for tests/demos). */
    public synchronized void flush() {
        sealActive();
        openWindow(windowStart + windowMillis);
    }

    public synchronized long currentWindowStart() {
        return windowStart;
    }

    public synchronized CountMinSketch currentSketch() {
        return activeSketch;
    }

    public synchronized BoundedCandidates currentCandidates() {
        return activeCandidates;
    }

    /** Sealed windows in chronological order (oldest first). */
    public synchronized List<WindowSummary> sealedWindows() {
        return new ArrayList<>(sealed);
    }

    /**
     * The most recently sealed window, or null if none has sealed yet.
     */
    public synchronized WindowSummary lastWindow() {
        return sealed.peekLast();
    }

    /**
     * Merge a partition's window summary into our sealed window with the same
     * [start,end) range. Sketch geometry/seed mismatch is rejected.
     *
     * @return the merged window summary
     * @throws IllegalArgumentException if no matching window exists
     */
    public synchronized WindowSummary mergeWindow(WindowSummary other) {
        WindowSummary target = null;
        for (WindowSummary s : sealed) {
            if (s.startMillis() == other.startMillis() && s.endMillis() == other.endMillis()) {
                target = s;
                break;
            }
        }
        if (target == null) {
            throw new IllegalArgumentException(
                    "no local window matches [" + other.startMillis() + "," + other.endMillis() + ")");
        }
        target.sketch().mergeWith(other.sketch());
        target.candidates().mergeWith(other.candidates());
        return target;
    }

    /**
     * Build a fresh standalone sketch summing all sealed windows in
     * {@code [fromMillis, toMillis)} plus, when inclusive, the active window.
     * Candidate sets are intersected with keys actually retained per window.
     */
    public synchronized CountMinSketch rangeSketch(long fromMillis, long toMillis,
                                                    boolean includeActive) {
        CountMinSketch merged = new CountMinSketch(width, depth, seed);
        for (WindowSummary s : sealed) {
            if (s.startMillis() >= fromMillis && s.endMillis() <= toMillis) {
                merged.mergeWith(s.sketch());
            }
        }
        if (includeActive && windowStart >= fromMillis && windowStart + windowMillis <= toMillis) {
            merged.mergeWith(activeSketch);
        }
        return merged;
    }

    /** Top-K over the active window. */
    public synchronized List<Map.Entry<String, Long>> currentTopK(int k) {
        return activeCandidates.topK(k);
    }

    public long windowMillis() {
        return windowMillis;
    }

    public long seed() {
        return seed;
    }

    public int sketchWidth() {
        return width;
    }

    public int sketchDepth() {
        return depth;
    }

    public int candidateCapacity() {
        return candidateCapacity;
    }

    /** Stop periodic sealing (production lifecycle). */
    public synchronized void shutdown() {
        if (scheduler != null && scheduledTaskId >= 0) {
            scheduler.cancel(scheduledTaskId);
            scheduledTaskId = -1;
        }
    }
}

package orderedevents.model;

import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Mutable record of one event, owned by exactly one {@code Partition}.
 *
 * <p>All state transitions happen while holding the partition lock. The
 * volatile fields let HTTP threads observe the latest state/timestamps without
 * taking the lock for read-only views; {@code cancelRequested} is an atomic
 * flag checked by the worker thread (cooperative cancellation of a running
 * attempt and of tasks queued on the concurrency semaphore).
 */
public final class EventRecord {

    public final String id;
    public final String partition;
    public final long seq;
    public final EventSpec spec;
    public final long enqueueNanos;
    public final long enqueueEpochMillis;
    public final long timeoutMillis;   // effective per-attempt timeout
    public final int maxAttempts;      // effective total attempts

    public volatile EventState state = EventState.PENDING;
    public volatile long startNanos;          // 0 until first attempt starts
    public volatile long endNanos;            // 0 until terminal (non-cancel) state
    public volatile long endEpochMillis;      // wall-clock when settlement occurred
    public volatile long committedEpochMillis; // wall-clock when committed in order
    public volatile int attemptCount;         // attempts actually performed
    public volatile String lastError;         // failure detail for the placeholder
    public volatile Object successValue;      // value on success
    public volatile Thread workerThread;      // thread running the event worker

    /** Set by a cancel request; the worker checks it at each await point. */
    public final AtomicBoolean cancelRequested = new AtomicBoolean(false);
    /** Set once a cancel request won the race and the event is irreversibly cancelled. */
    public final AtomicBoolean cancelSealed = new AtomicBoolean(false);

    public EventRecord(String id, String partition, long seq, EventSpec spec,
                       long timeoutMillis, int maxAttempts, long nowNanos, long nowMillis) {
        this.id = id;
        this.partition = partition;
        this.seq = seq;
        this.spec = spec;
        this.timeoutMillis = timeoutMillis;
        this.maxAttempts = maxAttempts;
        this.enqueueNanos = nowNanos;
        this.enqueueEpochMillis = nowMillis;
    }

    /** True once a result/tombstone is sitting in the partition slot map. */
    public boolean isTerminalInSlot() {
        EventState s = state;
        return s == EventState.SUCCEEDED || s == EventState.FAILED
                || s == EventState.TIMED_OUT || s == EventState.CANCELLED;
    }

    public boolean isCommitted() {
        return state == EventState.COMMITTED;
    }
}

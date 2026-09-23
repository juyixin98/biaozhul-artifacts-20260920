package orderedevents.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.Semaphore;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;

import orderedevents.model.EventRecord;
import orderedevents.model.EventSpec;
import orderedevents.model.EventState;
import orderedevents.model.ResultEntry;

/**
 * A single ordering domain.
 *
 * <p>Guarantees:
 * <ul>
 *   <li><b>ordered commit</b>: results are appended to {@link #results} strictly
 *       in {@code seq} order; a still-unsettled head event is a barrier behind
 *       which already-settled later events wait;</li>
 *   <li><b>failure placeholders</b>: exhausted/timeout events commit entries
 *       with status FAILED/TIMED_OUT, exactly like successes, so order never
 *       silently skips them;</li>
 *   <li><b>cancellation</b>: a cancelled event leaves no entry; its slot is
 *       removed and the barrier opens for the events behind it;</li>
 *   <li><b>buffer cap</b>: at most {@code maxInFlight} admitted events
 *       (buffered + running) exist at once; further submits are rejected;</li>
 *   <li><b>concurrency cap</b>: at most {@code maxConcurrent} attempts execute
 *       at the same time in this partition; the rest wait buffered.</li>
 * </ul>
 *
 * All mutating state is guarded by {@link #lock}.
 */
final class Partition {

    final String name;

    private final ReentrantLock lock = new ReentrantLock();
    private final Condition changed = lock.newCondition();

    /** Seq -> record for admitted, not-yet-committed/removed events. */
    private final TreeMap<Long, EventRecord> slots = new TreeMap<>();
    /** Cancelled events retained for idempotent cancel and GET (bounded by throughput). */
    private final LinkedHashMap<String, EventRecord> cancelled = new LinkedHashMap<>();
    /** Committed output, in commit order (= seq order with cancelled gaps). */
    private final ArrayList<ResultEntry> results = new ArrayList<>();

    private long nextSeq;
    private int admitted;
    private int runningAttempts;

    private final Semaphore concurrency;
    private final int maxInFlight;
    private final int maxConcurrent;

    Partition(String name, ServerConfig config) {
        this.name = name;
        this.maxInFlight = config.maxInFlight();
        this.maxConcurrent = config.maxConcurrent();
        // Fair: events reach their attempt in submission order when permits free up.
        this.concurrency = new Semaphore(maxConcurrent, true);
    }

    ReentrantLock lock() {
        return lock;
    }

    Semaphore concurrency() {
        return concurrency;
    }

    /**
     * Admits one event. Rejects with 503 when the partition's in-flight buffer
     * is full (caller turns this into an HTTP 503).
     */
    EventRecord admit(EventSpec spec, long timeoutMillis, int maxAttempts) {
        lock.lock();
        try {
            if (admitted >= maxInFlight) {
                throw ApiException.unavailable(
                        "partition '" + name + "' in-flight buffer is full (limit " + maxInFlight + ")");
            }
            long seq = nextSeq++;
            EventRecord rec = new EventRecord(
                    java.util.UUID.randomUUID().toString(),
                    name, seq, spec, timeoutMillis, maxAttempts,
                    System.nanoTime(), System.currentTimeMillis());
            slots.put(seq, rec);
            admitted++;
            return rec;
        } finally {
            lock.unlock();
        }
    }

    /**
     * Opens the ordering barrier: while the head slot is settled (terminal) or
     * cancelled, remove it; settled heads append an output entry, cancelled
     * heads are skipped. Stops at the first still-pending/running head.
     * Caller must hold {@link #lock}.
     */
    void drainLocked() {
        long now = System.currentTimeMillis();
        while (!slots.isEmpty()) {
            EventRecord head = slots.firstEntry().getValue();
            EventState st = head.state;
            if (st == EventState.CANCELLED) {
                slots.pollFirstEntry();
                admitted--;
                rememberCancelledLocked(head);
                continue;
            }
            if (head.isTerminalInSlot()) {
                slots.pollFirstEntry();
                admitted--;
                // Capture the terminal status BEFORE flipping to COMMITTED —
                // the output entry must show SUCCEEDED/FAILED/TIMED_OUT.
                EventState terminalState = head.state;
                Object value = head.successValue;
                String error = head.lastError;
                int attempts = head.attemptCount;
                long endNanos = head.endNanos;
                long enqueueNanos = head.enqueueNanos;
                String id = head.id;
                long seq = head.seq;
                head.committedEpochMillis = now;
                head.state = EventState.COMMITTED;
                long durationMs = Math.max(0,
                        java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(endNanos - enqueueNanos));
                results.add(new ResultEntry(
                        id, seq, terminalState, value, error, attempts, durationMs, now));
                continue;
            }
            break;
        }
        changed.signalAll();
    }

    /** Removes a cancelled event's slot and re-opens the barrier. Caller holds lock. */
    void removeCancelledLocked(EventRecord rec) {
        if (slots.remove(rec.seq) != null) {
            admitted--;
            rememberCancelledLocked(rec);
        }
        drainLocked();
    }

    /** Retains cancelled records for idempotent cancel and GET, capped to avoid unbounded growth. */
    private void rememberCancelledLocked(EventRecord rec) {
        cancelled.putIfAbsent(rec.id, rec);
        while (cancelled.size() > 10_000) {
            var it = cancelled.entrySet().iterator();
            it.next();
            it.remove();
        }
    }

    /** Finds a cancelled record by id. Caller holds {@link #lock}. */
    EventRecord findCancelledLocked(String eventId) {
        return cancelled.get(eventId);
    }

    /** Finds an admitted (non-committed) event by id. Caller holds {@link #lock}. */
    EventRecord findSlotLocked(String eventId) {
        for (EventRecord rec : slots.values()) {
            if (rec.id.equals(eventId)) {
                return rec;
            }
        }
        return null;
    }

    /**
     * Long-polls until the committed sequence reaches {@code waitForSeq}
     * (i.e. at least one entry with seq >= waitForSeq exists), or the wait
     * elapses. Returns entries with seq greater than {@code afterSeq}.
     */
    List<ResultEntry> resultsAfter(long afterSeq, Long waitForSeq, long waitMillis)
            throws InterruptedException {
        lock.lock();
        try {
            long deadline = System.nanoTime()
                    + java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(Math.max(0, waitMillis));
            while (waitMillis > 0 && deadline - System.nanoTime() > 0) {
                if (waitForSeq != null && lastSeqLocked() >= waitForSeq) {
                    break;
                }
                if (waitForSeq == null && lastSeqLocked() > afterSeq) {
                    break;
                }
                changed.awaitNanos(deadline - System.nanoTime());
            }
            List<ResultEntry> out = new ArrayList<>();
            for (ResultEntry e : results) {
                if (e.seq() > afterSeq) {
                    out.add(e);
                }
            }
            return out;
        } finally {
            lock.unlock();
        }
    }

    private long lastSeqLocked() {
        return results.isEmpty() ? -1L : results.get(results.size() - 1).seq();
    }

    void onAttemptStarted() {
        lock.lock();
        try {
            runningAttempts++;
        } finally {
            lock.unlock();
        }
    }

    void onAttemptFinished() {
        lock.lock();
        try {
            runningAttempts = Math.max(0, runningAttempts - 1);
        } finally {
            lock.unlock();
        }
    }

    Map<String, Object> status() {
        lock.lock();
        try {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("partition", name);
            m.put("nextSeq", nextSeq);
            m.put("committed", results.size());
            m.put("admitted", admitted);
            m.put("bufferedOrRunning", slots.size());
            m.put("inFlightLimit", maxInFlight);
            m.put("concurrencyLimit", maxConcurrent);
            m.put("runningAttempts", runningAttempts);
            return m;
        } finally {
            lock.unlock();
        }
    }

    /** Snapshot copy of all committed results. */
    List<ResultEntry> currentResults() {
        lock.lock();
        try {
            return new ArrayList<>(results);
        } finally {
            lock.unlock();
        }
    }
}

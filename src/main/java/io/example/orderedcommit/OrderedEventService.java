package io.example.orderedcommit;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

/**
 * The ordered asynchronous event-processing service.
 *
 * <p>Guarantees and semantics:
 * <ul>
 *   <li><b>Per-partition ordering.</b> Each submitted event gets a zero-based
 *       sequence number. A result is appended to a partition's committed output
 *       only once every lower-sequenced event has been resolved, so the output
 *       is in input order even when a later event finishes first
 *       (head-of-line buffering).</li>
 *   <li><b>Partition independence.</b> Partitions share only a global worker
 *       pool sized to the in-flight cap; a blocked head event in one partition
 *       never blocks another.</li>
 *   <li><b>In-flight cap.</b> The fixed worker pool bounds the number of
 *       events actually processing at once; overflow stays SCHEDULED and is
 *       accounted as buffered.</li>
 *   <li><b>Buffer cap.</b> Each partition accepts at most {@code bufferCap}
 *       unresolved events; a submit past the cap fails with HTTP 429 instead
 *       of growing memory without bound.</li>
 *   <li><b>Failure placeholder.</b> When all attempts are exhausted (or the
 *       last attempt times out) a FAILURE entry carrying the last error is
 *       committed in the event's sequence slot.</li>
 *   <li><b>Retry.</b> A failed attempt is retried up to {@code maxAttempts}
 *       total attempts after {@code retryDelayMillis}; each attempt has its own
 *       timeout.</li>
 *   <li><b>Cancel.</b> An event cancelled before it is finished (queued,
 *       retry-waiting or running) ends CANCELLED, occupies no output slot and
 *       produces no result; a running attempt is interrupted. Events already
 *       SUCCEEDED/FAILED/COMMITTED cannot be cancelled (409).</li>
 * </ul>
 */
public final class OrderedEventService {

    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9._-]{1,64}");

    /** Service-wide configuration. */
    public static final class Config {
        public final int inflightCap;
        public final long defaultBufferCap;
        public final long defaultDelayMillis;
        public final long defaultTimeoutMillis;
        public final int defaultMaxAttempts;
        public final long defaultRetryDelayMillis;

        public Config(
                int inflightCap,
                long defaultBufferCap,
                long defaultDelayMillis,
                long defaultTimeoutMillis,
                int defaultMaxAttempts,
                long defaultRetryDelayMillis) {
            if (inflightCap < 1) {
                throw new IllegalArgumentException("inflightCap must be >= 1");
            }
            if (defaultBufferCap < 1) {
                throw new IllegalArgumentException("defaultBufferCap must be >= 1");
            }
            if (defaultTimeoutMillis < 1) {
                throw new IllegalArgumentException("defaultTimeoutMillis must be >= 1");
            }
            if (defaultMaxAttempts < 1 || defaultMaxAttempts > 20) {
                throw new IllegalArgumentException("defaultMaxAttempts must be in [1,20]");
            }
            this.inflightCap = inflightCap;
            this.defaultBufferCap = defaultBufferCap;
            this.defaultDelayMillis = defaultDelayMillis;
            this.defaultTimeoutMillis = defaultTimeoutMillis;
            this.defaultMaxAttempts = defaultMaxAttempts;
            this.defaultRetryDelayMillis = defaultRetryDelayMillis;
        }

        public static Config defaults() {
            // inflight 8, per-partition buffer 16, simulated latency 500ms,
            // attempt timeout 2s, 3 attempts, 100ms between attempts.
            return new Config(8, 16, 500, 2000, 3, 100);
        }
    }

    private final Config config;
    private final EventProcessor processor;

    /** Core pool == max pool == cap: at most inflightCap events run at once. */
    private final ThreadPoolExecutor workers;
    private final ScheduledExecutorService scheduler;

    private final Object partitionsLock = new Object();
    private final Map<String, Partition> partitions = new LinkedHashMap<>();

    public OrderedEventService(Config config, EventProcessor processor) {
        this.config = config;
        this.processor = processor;
        ThreadFactory workerFactory = new NamedThreadFactory("event-worker");
        this.workers =
                (ThreadPoolExecutor)
                        Executors.newFixedThreadPool(config.inflightCap, workerFactory);
        this.scheduler =
                Executors.newScheduledThreadPool(
                        Math.max(2, config.inflightCap / 2),
                        new NamedThreadFactory("event-timer"));
    }

    // ------------------------------------------------------------------
    // Partition management
    // ------------------------------------------------------------------

    public Map<String, Object> createPartition(String name, Long bufferCap) {
        validateName(name);
        long cap = bufferCap != null ? bufferCap : config.defaultBufferCap;
        if (cap < 1 || cap > 1_000_000) {
            throw EventException.badRequest("bufferCap must be in [1,1000000]");
        }
        synchronized (partitionsLock) {
            Partition existing = partitions.get(name);
            if (existing != null) {
                return existing.stats();
            }
            Partition partition = new Partition(name, cap, System.currentTimeMillis());
            partitions.put(name, partition);
            return partition.stats();
        }
    }

    public List<Map<String, Object>> listPartitions() {
        synchronized (partitionsLock) {
            List<Map<String, Object>> list = new ArrayList<>(partitions.size());
            for (Partition p : partitions.values()) {
                synchronized (p) {
                    list.add(p.stats());
                }
            }
            return list;
        }
    }

    public Map<String, Object> partitionStats(String name) {
        return requirePartition(name).stats();
    }

    private Partition requirePartition(String name) {
        validateName(name);
        synchronized (partitionsLock) {
            Partition p = partitions.get(name);
            if (p == null) {
                throw EventException.notFound("partition not found: " + name);
            }
            return p;
        }
    }

    private static void validateName(String name) {
        if (name == null || !NAME.matcher(name).matches()) {
            throw EventException.badRequest(
                    "partition name must match [A-Za-z0-9._-]{1,64}");
        }
    }

    // ------------------------------------------------------------------
    // Submit
    // ------------------------------------------------------------------

    public Map<String, Object> submit(String partitionName, Map<String, Object> body) {
        Partition partition = requirePartition(partitionName);

        long delay = longField(body, "delayMillis", config.defaultDelayMillis);
        long timeout = longField(body, "timeoutMillis", config.defaultTimeoutMillis);
        long retryDelay = longField(body, "retryDelayMillis", config.defaultRetryDelayMillis);
        int maxAttempts = intField(body, "maxAttempts", config.defaultMaxAttempts);
        boolean fail = Boolean.TRUE.equals(body.get("fail"));
        Object payload = body.get("payload");

        if (delay < 0 || delay > 600_000) {
            throw EventException.badRequest("delayMillis must be in [0,600000]");
        }
        if (timeout < 1 || timeout > 600_000) {
            throw EventException.badRequest("timeoutMillis must be in [1,600000]");
        }
        if (retryDelay < 0 || retryDelay > 600_000) {
            throw EventException.badRequest("retryDelayMillis must be in [0,600000]");
        }
        if (maxAttempts < 1 || maxAttempts > 20) {
            throw EventException.badRequest("maxAttempts must be in [1,20]");
        }

        Event event;
        synchronized (partition) {
            if (partition.outstanding() >= partition.bufferCap) {
                throw EventException.tooManyRequests(
                        "partition buffer full: cap="
                                + partition.bufferCap
                                + " outstanding="
                                + partition.outstanding());
            }
            event = partition.assign(payload, delay, fail, timeout, maxAttempts, retryDelay);
            partition.eventsById.put(event.id, event);
        }
        dispatch(event);
        return event.toMap();
    }

    private static long longField(Map<String, Object> body, String key, long fallback) {
        Object v = body.get(key);
        if (v == null || v == Json.NULL) {
            return fallback;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        try {
            return Long.parseLong(String.valueOf(v));
        } catch (NumberFormatException e) {
            throw EventException.badRequest(key + " must be an integer");
        }
    }

    private static int intField(Map<String, Object> body, String key, int fallback) {
        long value = longField(body, key, fallback);
        if (value > Integer.MAX_VALUE) {
            throw EventException.badRequest(key + " too large");
        }
        return (int) value;
    }

    // ------------------------------------------------------------------
    // Reads
    // ------------------------------------------------------------------

    public Map<String, Object> getEvent(String partitionName, String eventId) {
        Partition partition = requirePartition(partitionName);
        Event event;
        synchronized (partition) {
            event = partition.eventsById.get(eventId);
        }
        if (event == null) {
            throw EventException.notFound("event not found: " + eventId);
        }
        return event.toMap();
    }

    /**
     * Committed output of a partition. {@code sinceSeq} filters to entries with
     * seq &gt; sinceSeq; {@code waitMillis} long-polls until new output exists.
     */
    public List<Map<String, Object>> results(String partitionName, long sinceSeq, long waitMillis)
            throws InterruptedException {
        Partition partition = requirePartition(partitionName);
        long deadline = System.currentTimeMillis() + Math.max(0, waitMillis);
        synchronized (partition) {
            while (lastSeq(partition) <= sinceSeq) {
                long remaining = deadline - System.currentTimeMillis();
                if (remaining <= 0) {
                    break;
                }
                partition.wait(remaining);
            }
            List<Map<String, Object>> out = new ArrayList<>();
            for (Committed c : partition.committed) {
                if (c.seq > sinceSeq) {
                    out.add(c.toMap());
                }
            }
            return out;
        }
    }

    private static long lastSeq(Partition partition) {
        if (partition.committed.isEmpty()) {
            return -1L;
        }
        return partition.committed.get(partition.committed.size() - 1).seq;
    }

    // ------------------------------------------------------------------
    // Cancel
    // ------------------------------------------------------------------

    public Map<String, Object> cancel(String partitionName, String eventId, String reason) {
        Partition partition = requirePartition(partitionName);
        Event event;
        synchronized (partition) {
            event = partition.eventsById.get(eventId);
        }
        if (event == null) {
            throw EventException.notFound("event not found: " + eventId);
        }

        boolean shouldPump = false;
        synchronized (event.lock) {
            if (event.status == Status.CANCELLED) {
                return event.toMap(); // idempotent
            }
            if (event.status != Status.SCHEDULED && event.status != Status.RUNNING) {
                throw EventException.conflict(
                        "event already finished with status " + event.status.name());
            }
            event.status = Status.CANCELLED;
            event.cancelReason = reason != null ? reason : "cancelled via API";
            event.finishedAt = System.currentTimeMillis();
            event.inFlight = false;
            event.timedOut = false;
            cancelQuietly(event.timeoutFuture);
            cancelQuietly(event.wakeFuture);
            if (event.workerFuture != null) {
                // Remove a still-queued attempt from the pool and interrupt a
                // running one; a queued task that slips through checks state.
                workers.remove(event.workerTask);
                event.workerFuture.cancel(true);
            }
            shouldPump = true;
        }
        if (shouldPump) {
            pump(partition);
        }
        return event.toMap();
    }

    // ------------------------------------------------------------------
    // Global stats
    // ------------------------------------------------------------------

    public Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("inflightCap", config.inflightCap);
        int partitionsCount;
        long submitted = 0;
        long committedCount = 0;
        int outstanding = 0;
        int inFlight = 0;
        synchronized (partitionsLock) {
            partitionsCount = partitions.size();
            for (Partition p : partitions.values()) {
                synchronized (p) {
                    submitted += p.nextSeq;
                    committedCount += p.committed.size();
                    outstanding += p.outstanding();
                    for (Event e : p.pending) {
                        if (e.inFlight) {
                            inFlight++;
                        }
                    }
                }
            }
        }
        m.put("partitions", partitionsCount);
        m.put("submitted", submitted);
        m.put("committed", committedCount);
        m.put("outstanding", outstanding);
        m.put("inFlight", inFlight);
        m.put("buffered", outstanding - inFlight);
        return m;
    }

    // ------------------------------------------------------------------
    // Worker state machine
    // ------------------------------------------------------------------

    private void dispatch(Event event) {
        event.workerTask = new AttemptTask(event);
        event.workerFuture = workers.submit(event.workerTask);
    }

    private final class AttemptTask implements Runnable {
        private final Event event;

        AttemptTask(Event event) {
            this.event = event;
        }

        @Override
        public void run() {
            synchronized (event.lock) {
                // Cancelled while sitting in the pool queue, or a duplicate dispatch.
                if (event.status != Status.SCHEDULED || event.inFlight) {
                    return;
                }
                event.status = Status.RUNNING;
                event.attempts++;
                event.inFlight = true;
                event.timedOut = false;
                event.lastError = null;
                event.timeoutFuture = armTimeout(event);
            }
            runAttempt(event);
        }
    }

    private ScheduledFuture<?> armTimeout(Event event) {
        return scheduler.schedule(
                () -> {
                    synchronized (event.lock) {
                        if (event.status == Status.RUNNING
                                && event.inFlight
                                && !event.timedOut) {
                            event.timedOut = true;
                            if (event.workerFuture != null) {
                                event.workerFuture.cancel(true);
                            }
                        }
                    }
                },
                event.attemptTimeoutMillis,
                TimeUnit.MILLISECONDS);
    }

    private void runAttempt(Event event) {
        Object output = null;
        Throwable failure = null;
        try {
            output = processor.process(event);
        } catch (Throwable t) {
            failure = t;
        }

        Partition partition;
        synchronized (partitionsLock) {
            partition = partitions.get(event.partition);
        }

        boolean retry = false;
        boolean pump = false;
        synchronized (event.lock) {
            cancelQuietly(event.timeoutFuture);
            event.timeoutFuture = null;
            event.inFlight = false;

            if (event.status == Status.CANCELLED) {
                // Cancellation interrupted this attempt; nothing more to do.
                return;
            }

            if (failure == null && event.timedOut) {
                // Timeout fired just as the attempt returned: count as timeout.
                failure = new Exception(
                        "attempt timed out after " + event.attemptTimeoutMillis + "ms");
            }

            if (failure == null) {
                event.status = Status.SUCCEEDED;
                event.result = output;
                event.finishedAt = System.currentTimeMillis();
                event.workerFuture = null;
                event.workerTask = null;
                pump = true;
            } else {
                event.lastError = describe(failure);
                if (event.attempts < event.maxAttempts) {
                    event.status = Status.SCHEDULED;
                    // Old attempt task is done; the retry-wait timer owns the event now.
                    event.workerFuture = null;
                    event.workerTask = null;
                    retry = true;
                } else {
                    event.status = Status.FAILED;
                    event.finishedAt = System.currentTimeMillis();
                    event.workerFuture = null;
                    event.workerTask = null;
                    pump = true;
                }
            }
        }

        if (retry) {
            scheduleRetry(event);
        } else if (pump && partition != null) {
            pump(partition);
        }
    }

    private void scheduleRetry(Event event) {
        event.wakeFuture =
                scheduler.schedule(
                        () -> {
                            synchronized (event.lock) {
                                if (event.status != Status.SCHEDULED || event.inFlight) {
                                    return; // cancelled (or impossible duplicate)
                                }
                                dispatch(event);
                            }
                        },
                        event.retryDelayMillis,
                        TimeUnit.MILLISECONDS);
    }

    private static String describe(Throwable t) {
        String msg = t.getMessage();
        return t.getClass().getSimpleName() + (msg != null ? ": " + msg : "");
    }

    /**
     * Append every finished head event in sequence order. Cancelled head events
     * are skipped (they occupy no output slot); SUCCEEDED becomes COMMITTED and
     * FAILED appends a failure placeholder. Stops at the first event still
     * scheduled or running — that is the head-of-line buffer.
     */
    private void pump(Partition partition) {
        boolean changed = false;
        synchronized (partition) {
            while (!partition.pending.isEmpty()) {
                Event head = partition.pending.get(0);
                synchronized (head.lock) {
                    if (head.status == Status.SCHEDULED || head.status == Status.RUNNING) {
                        break;
                    }
                    partition.pending.remove(0);
                    long now = System.currentTimeMillis();
                    if (head.status == Status.SUCCEEDED) {
                        head.status = Status.COMMITTED;
                        head.committedAt = now;
                        partition.committed.add(new Committed(head, true));
                        changed = true;
                    } else if (head.status == Status.FAILED) {
                        head.committedAt = now;
                        partition.committed.add(new Committed(head, false));
                        changed = true;
                    }
                    // CANCELLED: slot skipped, no output appended.
                }
            }
            if (changed) {
                partition.notifyAll();
            }
        }
    }

    private static void cancelQuietly(ScheduledFuture<?> future) {
        if (future != null) {
            future.cancel(false);
        }
    }

    public void shutdown() {
        workers.shutdownNow();
        scheduler.shutdownNow();
    }

    private static final class NamedThreadFactory implements ThreadFactory {
        private final String prefix;
        private final AtomicInteger counter = new AtomicInteger();

        NamedThreadFactory(String prefix) {
            this.prefix = prefix;
        }

        @Override
        public Thread newThread(Runnable r) {
            Thread t = new Thread(r, prefix + "-" + counter.incrementAndGet());
            t.setDaemon(true);
            return t;
        }
    }
}

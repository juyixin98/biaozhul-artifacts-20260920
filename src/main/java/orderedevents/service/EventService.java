package orderedevents.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import orderedevents.model.AttemptSpec;
import orderedevents.model.EventRecord;
import orderedevents.model.EventSpec;
import orderedevents.model.EventState;
import orderedevents.model.ResultEntry;

/**
 * The asynchronous ordered event-processing service.
 *
 * <p>Partitions are independent ordering domains created on first use. Every
 * submitted event gets a per-partition monotonically increasing sequence
 * number and is processed asynchronously; results become observable only via
 * the ordered commit barrier inside {@link Partition}.
 */
public final class EventService {

    private final ServerConfig config;
    private final ConcurrentHashMap<String, Partition> partitions = new ConcurrentHashMap<>();
    private final ExecutorService workers = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "event-worker");
        t.setDaemon(true);
        return t;
    });
    private final AttemptExecutor attempts = new AttemptExecutor();

    public EventService(ServerConfig config) {
        this.config = config;
    }

    // ---------------------------------------------------------------- submit

    /**
     * Validates and admits one event, then schedules its worker.
     *
     * @return snapshot describing the admitted event
     */
    public Map<String, Object> submit(String partitionName, Map<String, Object> body) {
        validatePartitionName(partitionName);
        EventSpec spec = parseSpec(body);
        long timeoutMillis = resolveTimeout(spec.timeoutMillis());
        int maxAttempts = resolveMaxAttempts(spec.maxAttempts());
        validateAttempts(spec.attempts());

        Partition p = partitions.computeIfAbsent(partitionName, n -> new Partition(n, config));
        EventRecord rec = p.admit(spec, timeoutMillis, maxAttempts);
        workers.submit(() -> runEvent(p, rec));
        return eventSnapshot(rec);
    }

    private EventSpec parseSpec(Map<String, Object> body) {
        if (body == null) {
            throw ApiException.badRequest("missing JSON body");
        }
        Object payload = body.getOrDefault("payload", null);

        List<AttemptSpec> attemptList = new ArrayList<>();
        Object rawAttempts = body.get("attempts");
        if (rawAttempts != null) {
            if (!(rawAttempts instanceof List<?> list)) {
                throw ApiException.badRequest("'attempts' must be an array");
            }
            for (Object item : list) {
                if (!(item instanceof Map<?, ?> am)) {
                    throw ApiException.badRequest("each attempt must be an object");
                }
                long delay = asLong(am.get("delayMillis"), 0L);
                String behavior = asString(am.get("behavior"), AttemptSpec.SUCCEED);
                String error = asString(am.get("error"), null);
                AttemptSpec a = new AttemptSpec(delay, behavior, error);
                if (!a.isValid()) {
                    throw ApiException.badRequest(
                            "invalid attempt: delayMillis=" + delay + ", behavior=" + behavior);
                }
                attemptList.add(a);
            }
        }
        if (attemptList.isEmpty()) {
            attemptList.add(new AttemptSpec(0L, AttemptSpec.SUCCEED, null));
        }

        Long timeout = asNullableLong(body.get("timeoutMillis"));
        Integer maxAttempts = asNullableInt(body.get("maxAttempts"));
        return new EventSpec(payload == null ? null : payload, attemptList, timeout, maxAttempts);
    }

    private long resolveTimeout(Long requested) {
        long t = requested == null ? config.defaultTimeoutMillis() : requested;
        if (t <= 0) {
            throw ApiException.badRequest("timeoutMillis must be positive");
        }
        if (t > config.maxTimeoutMillis()) {
            throw ApiException.badRequest("timeoutMillis exceeds cap " + config.maxTimeoutMillis());
        }
        return t;
    }

    private int resolveMaxAttempts(Integer requested) {
        int m = requested == null ? config.defaultMaxAttempts() : requested;
        if (m < 1) {
            throw ApiException.badRequest("maxAttempts must be >= 1");
        }
        if (m > config.maxAttemptsCap()) {
            throw ApiException.badRequest("maxAttempts exceeds cap " + config.maxAttemptsCap());
        }
        return m;
    }

    private void validateAttempts(List<AttemptSpec> attempts) {
        for (AttemptSpec a : attempts) {
            if (a.delayMillis() < 0 || a.delayMillis() > config.maxTimeoutMillis()) {
                throw ApiException.badRequest("attempt delayMillis out of range");
            }
        }
    }

    // ---------------------------------------------------------------- cancel

    /**
     * Requests cancellation. Semantics:
     * <ul>
     *   <li>already committed -> 409 (a submitted result can never be un-submitted);</li>
     *   <li>already cancelled -> idempotent 200;</li>
     *   <li>buffered (awaiting a concurrency permit) -> sealed immediately,
     *       slot removed, no result will ever be submitted;</li>
     *   <li>running -> worker interrupted at its next await point; the running
     *       attempt is abandoned, slot removed; if the attempt settles in the
     *       same instant the race is resolved in favour of whichever transition
     *       seals first.</li>
     * </ul>
     */
    public Map<String, Object> cancel(String partitionName, String eventId) {
        Partition p = requiredPartition(partitionName);
        EventRecord rec = findRecord(p, eventId);

        p.lock().lock();
        try {
            if (rec.isCommitted()) {
                throw ApiException.conflict("event already committed; cancellation refused");
            }
            if (rec.state == EventState.CANCELLED) {
                return eventSnapshot(rec);
            }
            rec.cancelRequested.set(true);
            rec.cancelSealed.set(true);
            rec.state = EventState.CANCELLED;
            rec.endNanos = System.nanoTime();
            rec.endEpochMillis = System.currentTimeMillis();
            rec.lastError = "cancelled";
            p.removeCancelledLocked(rec);
        } finally {
            p.lock().unlock();
        }
        Thread t = rec.workerThread;
        if (t != null) {
            t.interrupt();
        }
        return eventSnapshot(rec);
    }

    // ------------------------------------------------------------- queries

    public Map<String, Object> getEvent(String partitionName, String eventId) {
        Partition p = requiredPartition(partitionName);
        EventRecord rec = findRecord(p, eventId);
        p.lock().lock();
        try {
            return eventSnapshot(rec);
        } finally {
            p.lock().unlock();
        }
    }

    public List<ResultEntry> getResults(String partitionName, Long afterSeq, Long waitForSeq,
                                        Long waitMillis)
            throws InterruptedException {
        Partition p = requiredPartition(partitionName);
        long after = afterSeq == null ? -1L : afterSeq;
        long wait = waitMillis == null ? 0L : Math.min(Math.max(0, waitMillis), config.longPollCapMillis());
        if (after < -1) {
            throw ApiException.badRequest("afterSeq must be >= -1");
        }
        if (waitForSeq != null && waitForSeq < after) {
            throw ApiException.badRequest("waitForSeq must be >= afterSeq");
        }
        return p.resultsAfter(after, waitForSeq, wait);
    }

    public Map<String, Object> partitionStatus(String partitionName) {
        return requiredPartition(partitionName).status();
    }

    public List<String> listPartitions() {
        return new ArrayList<>(partitions.keySet().stream().sorted().toList());
    }

    // ------------------------------------------------------------- lifecycle

    public void shutdown() {
        workers.shutdownNow();
        attempts.shutdown();
    }

    // ============================================================== worker

    private void runEvent(Partition p, EventRecord rec) {
        // Workers come from a shared cached pool: a cancel-driven interrupt is
        // delivered to this thread, but the thread may be reused afterwards, so
        // never let an interrupt status leak across task boundaries.
        Thread.interrupted();
        rec.workerThread = Thread.currentThread();
        boolean acquired = false;
        try {
            // May have been cancelled while queued on the executor.
            if (rec.cancelRequested.get()) {
                return;
            }

            while (true) {
                if (rec.cancelRequested.get()) {
                    return;
                }
                try {
                    if (p.concurrency().tryAcquire(50, TimeUnit.MILLISECONDS)) {
                        acquired = true;
                        break;
                    }
                } catch (InterruptedException e) {
                    return; // cancel interrupt while queued for a permit
                }
            }

            if (rec.cancelRequested.get()) {
                return;
            }
            p.onAttemptStarted();
            try {
                processAttempts(p, rec);
            } finally {
                p.onAttemptFinished();
            }
        } catch (Throwable t) {
            if (!rec.cancelSealed.get()) {
                settleFailure(rec, EventState.FAILED, "worker error: " + t);
                drain(p, rec);
            }
        } finally {
            if (acquired) {
                p.concurrency().release();
            }
            rec.workerThread = null;
            Thread.interrupted();
        }
    }

    /** Executes attempts with retries until success, budget exhaustion, or cancellation. */
    private void processAttempts(Partition p, EventRecord rec) {
        List<AttemptSpec> plan = rec.spec.attempts();
        int budget = rec.maxAttempts;

        for (int attemptNo = 1; attemptNo <= budget; attemptNo++) {
            if (rec.cancelRequested.get()) {
                return;
            }
            if (attemptNo == 1) {
                rec.startNanos = System.nanoTime();
            }
            rec.attemptCount = attemptNo;
            AttemptSpec attempt = planFor(plan, attemptNo);
            AttemptExecutor.Outcome outcome =
                    attempts.run(attempt, rec.timeoutMillis, rec.spec.payload());

            // Race resolution: a cancel that sealed while the attempt was in
            // flight wins over every settlement below.
            if (rec.cancelSealed.get() || rec.cancelRequested.get()) {
                return;
            }

            if (outcome instanceof AttemptExecutor.Outcome.Succeeded s) {
                rec.successValue = s.value();
                settleTerminal(rec, EventState.SUCCEEDED, null);
                drain(p, rec);
                return;
            } else if (outcome instanceof AttemptExecutor.Outcome.Cancelled) {
                return;
            } else if (outcome instanceof AttemptExecutor.Outcome.Failed f) {
                rec.lastError = f.error();
                if (attemptNo == budget) {
                    settleTerminal(rec, EventState.FAILED, f.error());
                    drain(p, rec);
                    return;
                }
            } else if (outcome instanceof AttemptExecutor.Outcome.TimedOut t) {
                rec.lastError = t.error();
                if (attemptNo == budget) {
                    settleTerminal(rec, EventState.TIMED_OUT, t.error());
                    drain(p, rec);
                    return;
                }
            }

            // Fixed backoff before the retry, interruptible by cancellation.
            long backoff = config.retryBackoffMillis();
            if (backoff > 0 && attemptNo < budget) {
                try {
                    Thread.sleep(backoff);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
                if (rec.cancelRequested.get()) {
                    return;
                }
            }
        }
    }

    /** Repeats the last supplied attempt spec when the budget exceeds the plan length. */
    private static AttemptSpec planFor(List<AttemptSpec> plan, int attemptNo) {
        int idx = Math.min(attemptNo, plan.size()) - 1;
        return plan.get(idx);
    }

    private void settleTerminal(EventRecord rec, EventState state, String error) {
        rec.endNanos = System.nanoTime();
        rec.endEpochMillis = System.currentTimeMillis();
        rec.lastError = error;
        rec.state = state;
    }

    private void settleFailure(EventRecord rec, EventState state, String error) {
        rec.endNanos = System.nanoTime();
        rec.endEpochMillis = System.currentTimeMillis();
        rec.lastError = error;
        rec.state = state;
    }

    /** Places the settlement into the slot and drives the ordered-commit barrier. */
    private void drain(Partition p, EventRecord rec) {
        p.lock().lock();
        try {
            // A concurrently-sealed cancellation removed the slot already.
            if (rec.cancelSealed.get()) {
                return;
            }
            p.drainLocked();
        } finally {
            p.lock().unlock();
        }
    }

    // ------------------------------------------------------------- helpers

    private Partition requiredPartition(String name) {
        validatePartitionName(name);
        Partition p = partitions.get(name);
        if (p == null) {
            throw ApiException.notFound("partition '" + name + "' does not exist");
        }
        return p;
    }

    private EventRecord findRecord(Partition p, String eventId) {
        if (eventId == null || eventId.isBlank()) {
            throw ApiException.badRequest("event id is required");
        }
        // Linear lookup over admitted slots; partition sizes are bounded by maxInFlight.
        p.lock().lock();
        try {
            EventRecord found = p.findSlotLocked(eventId);
            if (found != null) {
                return found;
            }
            EventRecord wasCancelled = p.findCancelledLocked(eventId);
            if (wasCancelled != null) {
                return wasCancelled;
            }
        } finally {
            p.lock().unlock();
        }
        // Fallback: committed events remain discoverable for a short while via
        // a scan of results; the snapshot is reconstructed from the entry.
        for (ResultEntry e : p.currentResults()) {
            if (e.id().equals(eventId)) {
                return committedStub(p, e);
            }
        }
        throw ApiException.notFound("event '" + eventId + "' not found in partition");
    }

    private EventRecord committedStub(Partition p, ResultEntry e) {
        EventRecord stub = new EventRecord(
                e.id(), p.name, e.seq(), null, 0, e.attempts(), 0, e.committedAtMillis());
        stub.state = EventState.COMMITTED;
        stub.endEpochMillis = e.committedAtMillis();
        stub.attemptCount = e.attempts();
        stub.lastError = e.error();
        stub.successValue = e.value();
        stub.committedEpochMillis = e.committedAtMillis();
        return stub;
    }

    private Map<String, Object> eventSnapshot(EventRecord rec) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", rec.id);
        m.put("partition", rec.partition);
        m.put("seq", rec.seq);
        m.put("state", rec.state.name());
        m.put("attemptsPlanned", rec.maxAttempts);
        m.put("attemptsPerformed", rec.attemptCount);
        m.put("timeoutMillis", rec.timeoutMillis);
        m.put("enqueuedAtMillis", rec.enqueueEpochMillis);
        if (rec.startNanos != 0) {
            m.put("started", true);
        }
        if (rec.endNanos != 0 && rec.state != EventState.COMMITTED) {
            m.put("settledAtMillis", rec.endEpochMillis);
        }
        if (rec.committedEpochMillis != 0) {
            m.put("committedAtMillis", rec.committedEpochMillis);
        }
        if (rec.lastError != null) {
            m.put("error", rec.lastError);
        }
        if (rec.state == EventState.COMMITTED && rec.successValue != null) {
            m.put("value", rec.successValue);
        }
        if (rec.spec != null && rec.spec.attempts() != null) {
            List<Map<String, Object>> plan = new ArrayList<>();
            for (AttemptSpec a : rec.spec.attempts()) {
                Map<String, Object> am = new LinkedHashMap<>();
                am.put("delayMillis", a.delayMillis());
                am.put("behavior", a.behavior());
                if (a.error() != null) {
                    am.put("error", a.error());
                }
                plan.add(am);
            }
            m.put("plan", plan);
        }
        return m;
    }

    private static void validatePartitionName(String name) {
        if (name == null || name.isBlank()) {
            throw ApiException.badRequest("partition name is required");
        }
        boolean shapeOk = name.length() <= 128
                && name.matches("[A-Za-z0-9._-]+")
                && !name.equals(".") && !name.equals("..")
                && !name.contains("..")
                && !name.startsWith("-");
        if (!shapeOk) {
            throw ApiException.badRequest(
                    "invalid partition name (allowed: letters, digits, dot, underscore, dash; "
                            + "no '..'; <=128 chars)");
        }
    }

    private static long asLong(Object v, long dflt) {
        if (v == null) {
            return dflt;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw ApiException.badRequest("expected integer number, got: " + v);
    }

    private static Long asNullableLong(Object v) {
        return v == null ? null : asLong(v, 0L);
    }

    private static Integer asNullableInt(Object v) {
        return v == null ? null : (int) asLong(v, 0L);
    }

    private static String asString(Object v, String dflt) {
        if (v == null) {
            return dflt;
        }
        if (v instanceof String s) {
            return s;
        }
        throw ApiException.badRequest("expected string, got: " + v);
    }
}

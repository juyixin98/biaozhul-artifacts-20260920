package io.example.orderedcommit;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Future;
import java.util.concurrent.ScheduledFuture;

/** A single event inside one partition. Mutable; every field read or changed under {@link #lock}. */
final class Event {

    final String partition;
    final String id;
    /** Zero-based input order within the partition; assigned at submit time. */
    final long seq;

    final Object payload;
    final long delayMillis;
    final boolean fail;
    final long attemptTimeoutMillis;
    final int maxAttempts;
    final long retryDelayMillis;

    final long submittedAt;

    /** Per-event monitor; also used by the committer pump to wait safely. */
    final Object lock = new Object();

    Status status = Status.SCHEDULED;
    int attempts;
    String lastError;
    Object result;
    long finishedAt;
    long committedAt;
    String cancelReason;

    /** Set while a worker thread owns the current attempt. */
    boolean inFlight;
    /** Set by the timeout guard so the worker knows its interruption was a timeout. */
    boolean timedOut;

    Future<?> workerFuture;
    Runnable workerTask;
    ScheduledFuture<?> wakeFuture;
    ScheduledFuture<?> timeoutFuture;

    Event(
            String partition,
            String id,
            long seq,
            Object payload,
            long delayMillis,
            boolean fail,
            long attemptTimeoutMillis,
            int maxAttempts,
            long retryDelayMillis,
            long submittedAt) {
        this.partition = partition;
        this.id = id;
        this.seq = seq;
        this.payload = payload;
        this.delayMillis = delayMillis;
        this.fail = fail;
        this.attemptTimeoutMillis = attemptTimeoutMillis;
        this.maxAttempts = maxAttempts;
        this.retryDelayMillis = retryDelayMillis;
        this.submittedAt = submittedAt;
    }

    boolean isTerminal() {
        return status == Status.COMMITTED || status == Status.FAILED || status == Status.CANCELLED;
    }

    /** JSON view of the event, including its current lifecycle fields. */
    Map<String, Object> toMap() {
        synchronized (lock) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", id);
            m.put("partition", partition);
            m.put("seq", seq);
            m.put("status", status.name());
            m.put("attempts", attempts);
            m.put("maxAttempts", maxAttempts);
            m.put("delayMillis", delayMillis);
            m.put("attemptTimeoutMillis", attemptTimeoutMillis);
            m.put("retryDelayMillis", retryDelayMillis);
            m.put("fail", fail);
            m.put("submittedAt", submittedAt);
            if (lastError != null) {
                m.put("lastError", lastError);
            }
            if (result != null) {
                m.put("result", result);
            }
            if (finishedAt > 0) {
                m.put("finishedAt", finishedAt);
            }
            if (committedAt > 0) {
                m.put("committedAt", committedAt);
            }
            if (cancelReason != null) {
                m.put("cancelReason", cancelReason);
            }
            m.put("inFlight", inFlight);
            return m;
        }
    }
}

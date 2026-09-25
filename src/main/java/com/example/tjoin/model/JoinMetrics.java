package com.example.tjoin.model;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Mutable counters describing operator behavior. Exposed for tests and for
 * the JSON status endpoint.
 */
public final class JoinMetrics {

    public final AtomicLong leftEventsAccepted = new AtomicLong();
    public final AtomicLong rightEventsAccepted = new AtomicLong();
    public final AtomicLong duplicatesDropped = new AtomicLong();
    public final AtomicLong lateDropped = new AtomicLong();
    public final AtomicLong bufferRejected = new AtomicLong();
    public final AtomicLong oldestEvicted = new AtomicLong();
    public final AtomicLong leftStateCleaned = new AtomicLong();
    public final AtomicLong rightStateCleaned = new AtomicLong();
    public final AtomicLong pairsEmitted = new AtomicLong();
    /** Pairs suppressed because they had already been emitted (redelivery). */
    public final AtomicLong pairsSuppressed = new AtomicLong();

    public long leftBuffered;
    public long rightBuffered;
    public long leftWatermark = Long.MIN_VALUE;
    public long rightWatermark = Long.MIN_VALUE;
    public boolean leftIdle;
    public boolean rightIdle;
}

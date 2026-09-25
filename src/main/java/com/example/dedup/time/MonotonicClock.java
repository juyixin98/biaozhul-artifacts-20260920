package com.example.dedup.time;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Monotone wrapper around a base clock. If the underlying clock moves
 * backwards, the last observed value is held, and the rollback is counted
 * ({@link #rollbackCount()}). Event-time watermarks are never derived from
 * this clock directly; it only guards processing-time scheduling.
 */
public final class MonotonicClock implements Clock {
    private final Clock base;
    private final AtomicLong last = new AtomicLong(Long.MIN_VALUE);
    private final AtomicLong rollbacks = new AtomicLong();

    public MonotonicClock(Clock base) {
        this.base = base;
    }

    @Override
    public long currentTimeMillis() {
        long t = base.currentTimeMillis();
        while (true) {
            long cur = last.get();
            if (t < cur) {
                rollbacks.incrementAndGet();
                return cur;
            }
            if (last.compareAndSet(cur, t)) {
                return t;
            }
        }
    }

    public long rollbackCount() {
        return rollbacks.get();
    }
}

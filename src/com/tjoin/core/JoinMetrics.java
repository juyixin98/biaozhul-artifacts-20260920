package com.tjoin.core;

import java.util.concurrent.atomic.AtomicLong;

/**
 * 连接算子的运行指标（线程安全计数器）。
 */
public final class JoinMetrics {

    private final AtomicLong leftReceived = new AtomicLong();
    private final AtomicLong rightReceived = new AtomicLong();
    private final AtomicLong duplicates = new AtomicLong();
    private final AtomicLong lateDropped = new AtomicLong();
    private final AtomicLong emitted = new AtomicLong();
    private final AtomicLong leftExpired = new AtomicLong();
    private final AtomicLong rightExpired = new AtomicLong();
    private final AtomicLong staleWatermarks = new AtomicLong();

    public void incrementReceived(StreamSide side) {
        (side == StreamSide.LEFT ? leftReceived : rightReceived).incrementAndGet();
    }

    public void incrementDuplicates() {
        duplicates.incrementAndGet();
    }

    public void incrementLateDropped() {
        lateDropped.incrementAndGet();
    }

    public void incrementEmitted() {
        emitted.incrementAndGet();
    }

    public void incrementExpired(StreamSide bufferedSide) {
        (bufferedSide == StreamSide.LEFT ? leftExpired : rightExpired).incrementAndGet();
    }

    public void incrementStaleWatermark() {
        staleWatermarks.incrementAndGet();
    }

    public long leftReceived() {
        return leftReceived.get();
    }

    public long rightReceived() {
        return rightReceived.get();
    }

    public long duplicates() {
        return duplicates.get();
    }

    public long lateDropped() {
        return lateDropped.get();
    }

    public long emitted() {
        return emitted.get();
    }

    public long leftExpired() {
        return leftExpired.get();
    }

    public long rightExpired() {
        return rightExpired.get();
    }

    public long staleWatermarks() {
        return staleWatermarks.get();
    }

    @Override
    public String toString() {
        return "JoinMetrics{leftReceived=" + leftReceived
                + ", rightReceived=" + rightReceived
                + ", duplicates=" + duplicates
                + ", lateDropped=" + lateDropped
                + ", emitted=" + emitted
                + ", leftExpired=" + leftExpired
                + ", rightExpired=" + rightExpired
                + ", staleWatermarks=" + staleWatermarks + "}";
    }
}

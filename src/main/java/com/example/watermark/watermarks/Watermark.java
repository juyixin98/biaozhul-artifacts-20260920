package com.example.watermark.watermarks;

/**
 * Watermark value object. {@link #NO_WATERMARK} (Long.MIN_VALUE) means no
 * watermark has yet been emitted by a partition / globally.
 */
public final class Watermark {

    /** Sentinel: no watermark observed yet. */
    public static final long NO_WATERMARK = Long.MIN_VALUE;

    private final long timestamp;

    public Watermark(long timestamp) {
        this.timestamp = timestamp;
    }

    public long getTimestamp() {
        return timestamp;
    }

    public boolean isNoWatermark() {
        return timestamp == NO_WATERMARK;
    }

    @Override
    public boolean equals(Object o) {
        return (o instanceof Watermark w) && w.timestamp == timestamp;
    }

    @Override
    public int hashCode() {
        return Long.hashCode(timestamp);
    }

    @Override
    public String toString() {
        return isNoWatermark() ? "Watermark[NO_WATERMARK]" : "Watermark[" + timestamp + ']';
    }
}

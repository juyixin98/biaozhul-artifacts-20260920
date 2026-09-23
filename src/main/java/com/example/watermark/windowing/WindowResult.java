package com.example.watermark.windowing;

import com.example.watermark.watermarks.StreamEvent;

/**
 * Result of one closed tumbling window.
 *
 * @param windowStart    inclusive start timestamp
 * @param windowEnd      exclusive end timestamp; the window closes once the
 *                       watermark reaches this value
 * @param partitionKey   partition that produced the window
 * @param eventCount     on-time events collected in the window
 * @param payloads       payloads of those events, in arrival order
 */
public record WindowResult(
        long windowStart,
        long windowEnd,
        String partitionKey,
        int eventCount,
        java.util.List<Object> payloads) {
}

package com.example.watermark.watermarks;

/**
 * An event in the stream.
 *
 * @param timestamp event-time timestamp (epoch millis)
 * @param key       partition/key this event belongs to
 * @param payload   arbitrary user data (for the JSON service: a parsed JSON value)
 */
public record StreamEvent(long timestamp, String key, Object payload) {
}

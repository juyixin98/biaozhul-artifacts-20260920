package com.example.watermark.watermarks;

/**
 * A record of an event diverted to the late channel: it arrived at a partition
 * whose global watermark had already passed its event-time timestamp.
 *
 * @param event                      the late event
 * @param globalWatermark            global watermark at arrival
 * @param arrivalProcessingTimeMillis processing-time clock at arrival
 * @param fromResumedPartition       true when the late event arrived as the
 *                                   first event of a partition resuming from idle
 */
public record LateEvent(
        StreamEvent event,
        long globalWatermark,
        long arrivalProcessingTimeMillis,
        boolean fromResumedPartition) {
}

package com.example.tjoin.model;

/**
 * Outcome of feeding one event into the join operator.
 */
public enum AcceptStatus {
    /** Event was processed normally (matched zero or more opposite events). */
    ACCEPTED,
    /** Duplicate event id had already been seen on that side; ignored. */
    DUPLICATE,
    /** Event time was behind the side's current watermark; dropped as late. */
    LATE,
    /** Side buffer was full with {@link BufferOverflowPolicy#REJECT}; not admitted. */
    BUFFER_FULL
}

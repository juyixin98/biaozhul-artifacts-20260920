package com.example.tjoin.model;

/**
 * What the operator should do when an incoming event would make a side's
 * buffered-state count exceed its configured {@code maxBufferSize}.
 *
 * <ul>
 *   <li>{@link #REJECT} — refuse the new event (the caller can report it);
 *       existing buffered events are kept, so no matchable record is ever
 *       discarded by the buffer bound.</li>
 *   <li>{@link #DROP_OLDEST} — evict the buffered event with the smallest
 *       event time on that side to make room. This bounds memory but can
 *       lose a record that was still theoretically matchable, so it is
 *       opt-in and the eviction is counted.</li>
 * </ul>
 */
public enum BufferOverflowPolicy {
    REJECT,
    DROP_OLDEST
}

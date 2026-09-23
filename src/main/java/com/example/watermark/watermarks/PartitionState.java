package com.example.watermark.watermarks;

/**
 * Lifecycle state of a partition.
 * <ul>
 *   <li>{@link #WAITING} — registered but has never received an event; excluded
 *       from the global minimum and not considered idled.</li>
 *   <li>{@link #ACTIVE} — has received at least one event and is feeding the
 *       global watermark.</li>
 *   <li>{@link #IDLE} — was active, then received no event for the configured
 *       idle timeout; excluded from the global minimum until a new event arrives.</li>
 * </ul>
 */
public enum PartitionState {
    WAITING,
    ACTIVE,
    IDLE
}

package orderedevents.model;

/**
 * Lifecycle state of a submitted event.
 *
 * <pre>
 * PENDING  -> RUNNING -> SUCCEEDED / FAILED / TIMED_OUT
 * PENDING/RUNNING/SUCCEEDED/FAILED/TIMED_OUT -> CANCELLED (only before commit)
 * any terminal non-cancelled state -> COMMITTED (output delivered, in order)
 * </pre>
 *
 * COMMITTED is terminal and can never be cancelled: an event whose result has
 * already been submitted to the partition output cannot be un-submitted.
 */
public enum EventState {
    PENDING,
    RUNNING,
    SUCCEEDED,
    FAILED,
    TIMED_OUT,
    CANCELLED,
    COMMITTED
}

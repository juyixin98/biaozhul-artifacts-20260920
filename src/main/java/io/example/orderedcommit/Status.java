package io.example.orderedcommit;

/**
 * Status of an event in its lifecycle.
 *
 * <pre>
 *   SCHEDULED ──> RUNNING ──attempt fails, attempts remain──> SCHEDULED (retry wait)
 *                    │
 *                    ├─> SUCCEEDED   (last attempt ok; waiting its turn in the buffer)
 *                    ├─> COMMITTED   (success result appended to partition output)
 *                    ├─> FAILED      (attempts exhausted; FAILURE placeholder appended)
 *                    └─> CANCELLED   (cancel requested before it finished; slot skipped)
 * </pre>
 *
 * <p>SUCCEEDED means the work is finished but the partition head has not
 * released the slot yet (head-of-line buffering); once appended it becomes
 * COMMITTED. FAILED is itself terminal and means the failure placeholder has
 * been committed — success and failure therefore have distinct terminal
 * states, and the results endpoint marks each entry's {@code outcome}. A
 * CANCELLED event occupies no output slot: its place is skipped, so later
 * events are not blocked.
 */
public enum Status {
    SCHEDULED,
    RUNNING,
    SUCCEEDED,
    FAILED,
    COMMITTED,
    CANCELLED
}

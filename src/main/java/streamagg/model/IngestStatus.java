package streamagg.model;

/** Outcome of feeding one operation to the engine. */
public enum IngestStatus {
    /** Applied immediately (possibly after draining previously buffered operations). */
    APPLIED,
    /** Cannot resolve yet (out-of-order version, or RETRACT/CORRECT before ADD); buffered. */
    BUFFERED,
    /** Same opId or identical operation seen already; state untouched. */
    DUPLICATE,
    /** Conflicts with an already applied version (same version, different content); rejected. */
    CONFLICT,
    /** Semantic validation failure (e.g. ADD without value). */
    INVALID,
    /** Arrived later than the watermark allows; rejected. */
    LATE
}

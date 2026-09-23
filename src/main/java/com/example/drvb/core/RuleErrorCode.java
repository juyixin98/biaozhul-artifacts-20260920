package com.example.drvb.core;

/**
 * Rule registry failure codes. Carried as a stable machine-readable
 * {@link #code()} on every rule-publication or lookup failure so the JSON
 * service can map them to HTTP status codes.
 */
public enum RuleErrorCode {
    /** A version with the same id already exists. */
    DUPLICATE_VERSION,
    /** The very first version must be published via the bootstrap call. */
    NOT_BOOTSTRAPPED,
    /** Bootstrap was already performed. */
    ALREADY_BOOTSTRAPPED,
    /** A new version's {@code effectiveFrom} must be strictly later than the
     *  previous version's effective time (no overlapping or out-of-order
     *  intervals; historical bindings are immutable). */
    EFFECTIVE_TIME_IN_PAST,
    /** A rollback references a version id that does not exist (or was reclaimed). */
    SOURCE_VERSION_NOT_FOUND
}

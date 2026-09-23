package joinplanner.model;

/**
 * Where an edge's selectivity came from. Lets the plan explain every number it uses.
 */
public enum SelectivitySource {
    /** Caller supplied selectivity explicitly on the edge. */
    GIVEN,
    /** Computed from caller-supplied distinct-value counts (ndvLeft/ndvRight). */
    DERIVED,
    /** 1/max(cardinalityL, cardinalityR): columns assumed unique, nothing supplied. */
    ASSUMED_UNIQUE,
    /** 1/max(ndvL, ndvR): caller supplied one NDV, the other side was assumed unique. */
    ASSUMED_UNIQUE_OTHER_SIDE,
    /** Global fallback default selectivity (e.g. caller configured defaultSelectivity). */
    DEFAULT_FALLBACK
}

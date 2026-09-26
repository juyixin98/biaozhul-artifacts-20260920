package com.example.dstexpand.model;

/**
 * Policy for resolving a local time that falls in a DST gap (does not exist)
 * or overlap (exists twice).
 *
 * <p>EARLIER: resolve to the earlier UTC instant.
 * For a gap this uses the post-transition offset (lands just before the gap in UTC);
 * for an overlap this picks the first occurrence of the local time.</p>
 *
 * <p>LATER: resolve to the later UTC instant.
 * For a gap this uses the pre-transition offset (lands just after the gap in UTC);
 * for an overlap this picks the second occurrence of the local time.</p>
 *
 * <p>SKIP: omit the occurrence and record it in the response's skipped list.</p>
 *
 * <p>ERROR: abort the whole expansion with an error.</p>
 */
public enum ResolutionPolicy {
    EARLIER,
    LATER,
    SKIP,
    ERROR
}

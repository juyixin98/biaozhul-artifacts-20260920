package com.dstexp.model;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * A single expanded occurrence.
 *
 * @param utcInstant    UTC instant, ISO-8601 with {@code Z}.
 * @param ruleId        producing rule id.
 * @param localDateTime local wall-clock time used in the expansion (retained verbatim,
 *                      even when it fell inside a gap).
 * @param offsetSeconds offset applied when mapping the local time to UTC.
 * @param kind          NORMAL | GAP_EARLIER | GAP_LATER | OVERLAP_EARLIER | OVERLAP_LATER.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record Occurrence(
        String utcInstant,
        String ruleId,
        String localDateTime,
        long offsetSeconds,
        String kind
) {
}

package com.dstexp.model;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * An occurrence that was skipped because the local time fell in a gap/overlap and the
 * matching policy was {@code SKIP}.
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record SkippedOccurrence(
        String ruleId,
        String localDateTime,
        String reason
) {
}

package com.example.drvb.stream;

import java.util.List;
import java.util.Set;

/**
 * Append-only store of accepted matches and rejected events, plus the set of
 * rule version ids currently referenced by retained results.
 *
 * <p>The reference set is the second precondition for historical-version
 * garbage collection (the first being the lateness horizon): a version whose
 * results are still retained cannot be reclaimed, because those results must
 * remain auditable against the exact rule content that produced them.
 */
public interface ResultStore {

    void appendAccepted(IngestResult result);

    void appendRejected(IngestResult result);

    List<IngestResult> query(long fromEventTimeInclusive, long toEventTimeExclusive);

    /** Version ids referenced by retained accepted results. */
    Set<String> referencedVersionIds();

    /**
     * Drops accepted/rejected records whose processing time is older than
     * {@code olderThanMillisCutoff}. Returns the number removed.
     */
    int purgeProcessedBefore(long cutoffMillis);

    int acceptedCount();

    int rejectedCount();

    List<IngestResult> allAccepted();

    List<IngestResult> allRejected();
}

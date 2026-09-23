package com.example.drvb.stream;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Small-data, in-memory {@link ResultStore}: exact reference implementation
 * with simple linear scans. Every retained accepted result pins the rule
 * version that produced it via {@link #referencedVersionIds()}.
 */
public final class InMemoryResultStore implements ResultStore {

    private final List<IngestResult> accepted = new ArrayList<>();
    private final List<IngestResult> rejected = new ArrayList<>();

    @Override
    public synchronized void appendAccepted(IngestResult result) {
        accepted.add(result);
    }

    @Override
    public synchronized void appendRejected(IngestResult result) {
        rejected.add(result);
    }

    @Override
    public synchronized List<IngestResult> query(long fromEventTimeInclusive,
                                                 long toEventTimeExclusive) {
        List<IngestResult> out = new ArrayList<>();
        for (IngestResult r : accepted) {
            long t = r.event().eventTime();
            if (t >= fromEventTimeInclusive && t < toEventTimeExclusive) {
                out.add(r);
            }
        }
        return List.copyOf(out);
    }

    @Override
    public synchronized Set<String> referencedVersionIds() {
        Set<String> ids = new HashSet<>();
        for (IngestResult r : accepted) {
            if (r.version() != null) {
                ids.add(r.version().versionId());
            }
        }
        return Set.copyOf(ids);
    }

    @Override
    public synchronized int purgeProcessedBefore(long cutoffMillis) {
        int before = accepted.size() + rejected.size();
        accepted.removeIf(r -> r.processedAt() < cutoffMillis);
        rejected.removeIf(r -> r.processedAt() < cutoffMillis);
        return before - accepted.size() - rejected.size();
    }

    @Override
    public synchronized int acceptedCount() {
        return accepted.size();
    }

    @Override
    public synchronized int rejectedCount() {
        return rejected.size();
    }

    @Override
    public synchronized List<IngestResult> allAccepted() {
        return List.copyOf(accepted);
    }

    @Override
    public synchronized List<IngestResult> allRejected() {
        return List.copyOf(rejected);
    }
}

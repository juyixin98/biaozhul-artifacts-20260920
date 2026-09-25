package streamagg.model;

import java.util.List;

/**
 * Result of one ingest call. When the newly applied (or buffered) operation causes a
 * dependency chain to drain, every operation that resolved during that drain is listed
 * in {@code drained} (the triggering op itself is {@code drained.get(0)} when
 * {@code status == APPLIED} and a chain was involved).
 */
public record IngestResult(
        IngestStatus status,
        String message,
        long appliedVersion,
        List<ResolvedOp> drained,
        int bufferedPending) {

    public static IngestResult applied(ResolvedOp self, List<ResolvedOp> drained, int bufferedPending) {
        return new IngestResult(IngestStatus.APPLIED, "applied", self.version(),
                drained, bufferedPending);
    }

    public static IngestResult buffered(String message, int bufferedPending) {
        return new IngestResult(IngestStatus.BUFFERED, message, -1, List.of(), bufferedPending);
    }

    public static IngestResult duplicate(String message) {
        return new IngestResult(IngestStatus.DUPLICATE, message, -1, List.of(), 0);
    }

    public static IngestResult conflict(String message) {
        return new IngestResult(IngestStatus.CONFLICT, message, -1, List.of(), 0);
    }

    public static IngestResult invalid(String message) {
        return new IngestResult(IngestStatus.INVALID, message, -1, List.of(), 0);
    }

    public static IngestResult late(String message) {
        return new IngestResult(IngestStatus.LATE, message, -1, List.of(), 0);
    }
}

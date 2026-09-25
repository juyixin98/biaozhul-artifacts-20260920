package streamagg.model;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

/** Immutable snapshot of an emitted output: per-key aggregates plus engine diagnostics. */
public record OutputSnapshot(
        long sequence,
        Long emittedAt,
        Long watermark,
        Map<String, BigDecimal> sums,
        Map<String, Long> counts,
        int liveEvents,
        int pendingOps,
        List<ResolvedOp> recentResolved) {
}

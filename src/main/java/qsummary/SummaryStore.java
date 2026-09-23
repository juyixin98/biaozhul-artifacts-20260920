package qsummary;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory registry of named summaries. The service is stateless across
 * restarts; snapshots provide persistence/transport.
 */
public final class SummaryStore {

    private final Map<String, GKQuantileSummary> summaries = new ConcurrentHashMap<>();

    private static final Set<String> RESERVED = Set.of();

    static void validateId(String id) {
        if (id == null || id.isBlank()) {
            throw new BadRequestException("summary id must not be empty");
        }
        if (id.length() > 128) {
            throw new BadRequestException("summary id too long (max 128)");
        }
        if (!id.matches("[A-Za-z0-9._-]+")) {
            throw new BadRequestException("summary id may contain only A-Z a-z 0-9 . _ -");
        }
    }

    public GKQuantileSummary create(String id, double epsilon) {
        validateId(id);
        GKQuantileSummary created = new GKQuantileSummary(epsilon);
        GKQuantileSummary existing = summaries.putIfAbsent(id, created);
        if (existing != null) {
            throw new BadRequestException("summary '" + id + "' already exists");
        }
        return created;
    }

    /**
     * Register an already-built summary under a new id (used by merge and
     * snapshot load). Fails if the id exists, so callers never overwrite.
     */
    GKQuantileSummary register(String id, GKQuantileSummary summary) {
        validateId(id);
        GKQuantileSummary existing = summaries.putIfAbsent(id, summary);
        if (existing != null) {
            throw new BadRequestException("summary '" + id + "' already exists");
        }
        return summary;
    }

    public GKQuantileSummary get(String id) {
        validateId(id);
        GKQuantileSummary s = summaries.get(id);
        if (s == null) {
            throw new NotFoundException("no summary '" + id + "'");
        }
        return s;
    }

    public boolean delete(String id) {
        validateId(id);
        return summaries.remove(id) != null;
    }

    public Map<String, Object> describe(String id, GKQuantileSummary s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("algorithm", GKQuantileSummary.ALGORITHM);
        m.put("version", GKQuantileSummary.FORMAT_VERSION);
        m.put("order", GKQuantileSummary.ORDER);
        m.put("epsilon", s.epsilon());
        m.put("count", s.count());
        m.put("storedTuples", s.storedTuples());
        m.put("errorBoundRank", errorBound(s.epsilon(), s.count()));
        return m;
    }

    /** Additive rank-error bound floor(epsilon*n). */
    public static long errorBound(double epsilon, long n) {
        return (long) Math.floor(epsilon * n);
    }

    public Set<String> ids() {
        return Set.copyOf(summaries.keySet());
    }
}

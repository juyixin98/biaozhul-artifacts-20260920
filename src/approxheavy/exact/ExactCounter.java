package approxheavy.exact;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Exact reference implementation for small datasets: plain hash-map counting.
 *
 * <p>Used by tests and the accuracy demo to measure the sketch's real error
 * and the candidate set's recall. Intentionally simple and allocation-heavy;
 * it is the ground truth, not the production path.
 */
public final class ExactCounter {
    private final Map<String, Long> counts = new HashMap<>();
    private long totalCount = 0L;

    public void add(String key) {
        add(key, 1L);
    }

    public void add(String key, long count) {
        counts.merge(key, count, Long::sum);
        totalCount += count;
    }

    public long countOf(String key) {
        return counts.getOrDefault(key, 0L);
    }

    public long totalCount() {
        return totalCount;
    }

    public int distinctKeys() {
        return counts.size();
    }

    public Map<String, Long> allCounts() {
        return new HashMap<>(counts);
    }

    /** Exact top-K with stable tie-break by key. */
    public List<Map.Entry<String, Long>> topK(int k) {
        PriorityQueue<Map.Entry<String, Long>> heap = new PriorityQueue<>(
                Comparator.comparingLong(Map.Entry<String, Long>::getValue)
                        .reversed()
                        .thenComparing(Map.Entry::getKey));
        heap.addAll(counts.entrySet());
        List<Map.Entry<String, Long>> result = new ArrayList<>();
        for (int i = 0; i < k && !heap.isEmpty(); i++) {
            result.add(heap.poll());
        }
        return result;
    }
}

package approx.cms;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
/**
 * Exact reference counter: a plain hash map of per-item counts.
 *
 * <p>Intended for small/medium data volumes (tests, acceptance experiments and
 * correctness baselines). It establishes the ground truth that the
 * Count-Min Sketch estimates are compared against.
 */
public final class ExactCounter {

    private final Map<String, Long> counts = new HashMap<>();
    private long totalCount;

    public synchronized void add(String item) {
        add(item, 1L);
    }

    public synchronized void add(String item, long count) {
        if (count <= 0) {
            throw new IllegalArgumentException("count must be positive");
        }
        counts.merge(item, count, Long::sum);
        totalCount += count;
    }

    public synchronized long countOf(String item) {
        return counts.getOrDefault(item, 0L);
    }

    public synchronized long totalCount() {
        return totalCount;
    }

    public synchronized int distinctItems() {
        return counts.size();
    }

    /** True top-K: descending exact count, ties broken lexicographically. */
    public synchronized List<Map.Entry<String, Long>> topK(int k) {
        List<Map.Entry<String, Long>> list = new ArrayList<>(counts.entrySet());
        list.sort(Map.Entry.<String, Long>comparingByValue(Comparator.reverseOrder())
                .thenComparing(Map.Entry::getKey));
        List<Map.Entry<String, Long>> result = new ArrayList<>();
        for (int i = 0; i < Math.min(k, list.size()); i++) {
            Map.Entry<String, Long> e = list.get(i);
            result.add(new HashMap.SimpleEntry<>(e.getKey(), e.getValue()));
        }
        return result;
    }

    public synchronized Map<String, Long> snapshot() {
        return new LinkedHashMap<>(counts);
    }
}

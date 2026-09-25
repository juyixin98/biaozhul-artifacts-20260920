package approxheavy.candidates;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Bounded candidate set for approximate heavy hitters.
 *
 * <p>This structure remembers at most {@code capacity} keys. The sketch is the
 * only authority on counts; this set only decides <em>which keys are worth
 * remembering</em>. When full, a new/returning key displaces the retained key
 * with the smallest current estimate (insertion order breaks ties, so keys are
 * never replaced by an equally-rare peer in the same batch).
 *
 * <p><strong>No coverage guarantee.</strong> A true top-K key that is never
 * retained (displaced before accumulating enough mass) is simply absent from
 * {@link #topK}. The service must not claim the candidate list covers all real
 * frequent items — only the sketch's per-key error bound is guaranteed.
 */
public final class BoundedCandidates {
    /** Read-only count source, normally the owning window's Count-Min Sketch. */
    @FunctionalInterface
    public interface CountSource {
        long estimate(String key);
    }

    private static final class Entry {
        final String key;
        final long sequence;

        Entry(String key, long sequence) {
            this.key = key;
            this.sequence = sequence;
        }
    }

    private final int capacity;
    private final CountSource countSource;
    private final Map<String, Entry> retained = new LinkedHashMap<>();
    private long nextSequence = 0;

    public BoundedCandidates(int capacity, CountSource countSource) {
        if (capacity < 1) {
            throw new IllegalArgumentException("capacity must be >= 1");
        }
        if (countSource == null) {
            throw new IllegalArgumentException("countSource required");
        }
        this.capacity = capacity;
        this.countSource = countSource;
    }

    /** Observe one occurrence of {@code key}, evicting the rarest key if full. */
    public synchronized void observe(String key) {
        if (retained.containsKey(key)) {
            return;
        }
        admit(key);
    }

    private void admit(String key) {
        if (retained.size() < capacity) {
            retained.put(key, new Entry(key, nextSequence++));
            return;
        }

        long incomingEstimate = countSource.estimate(key);
        String victim = null;
        long victimEstimate = Long.MAX_VALUE;
        long victimSequence = Long.MAX_VALUE;
        for (Entry e : retained.values()) {
            long est = countSource.estimate(e.key);
            if (est < victimEstimate || (est == victimEstimate && e.sequence < victimSequence)) {
                victimEstimate = est;
                victimSequence = e.sequence;
                victim = e.key;
            }
        }

        // Strictly better, or equal but later (FIFO tie-break): displace.
        if (incomingEstimate > victimEstimate
                || (incomingEstimate == victimEstimate && victim != null)) {
            retained.remove(victim);
            retained.put(key, new Entry(key, nextSequence++));
        }
    }

    /**
     * Top retained keys by sketch estimate, highest first, ties broken by
     * earliest insertion. At most {@code k} entries are returned.
     */
    public synchronized List<Map.Entry<String, Long>> topK(int k) {
        if (k < 0) {
            throw new IllegalArgumentException("k must be >= 0");
        }
        PriorityQueue<Map.Entry<String, Long>> heap = new PriorityQueue<>(
                Comparator
                        .comparingLong(Map.Entry<String, Long>::getValue)
                        .reversed()
                        .thenComparing(e -> retained.get(e.getKey()).sequence));
        for (String key : retained.keySet()) {
            heap.add(Map.entry(key, countSource.estimate(key)));
        }
        List<Map.Entry<String, Long>> result = new ArrayList<>();
        for (int i = 0; i < k && !heap.isEmpty(); i++) {
            result.add(heap.poll());
        }
        return result;
    }

    public synchronized boolean contains(String key) {
        return retained.containsKey(key);
    }

    public synchronized int size() {
        return retained.size();
    }

    public int capacity() {
        return capacity;
    }

    /** Merge another candidate set: union of retained keys under the same eviction policy. */
    public synchronized void mergeWith(BoundedCandidates other) {
        for (String key : other.retained.keySet()) {
            if (!retained.containsKey(key)) {
                admit(key);
            }
        }
    }
}

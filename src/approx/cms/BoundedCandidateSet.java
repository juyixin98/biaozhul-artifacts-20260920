package approx.cms;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Bounded set of candidate items tracked alongside a {@link CountMinSketch}.
 *
 * <p>The candidate set is a fixed-size <em>retention</em> structure, not a
 * frequency guarantee: when it is full and a new item arrives, the currently
 * lowest-estimated candidate (ties broken lexicographically for determinism)
 * is evicted to make room. This keeps memory bounded at
 * {@code O(capacity) + sketch size}.
 *
 * <p><b>Coverage is intentionally not guaranteed.</b> An item that is among the
 * true top-K at the end of a window may have been evicted earlier &mdash; for
 * example when sketch collisions inflated a low-frequency item enough to evict
 * a genuine heavy hitter, or simply when many items share a similar weight.
 * Point queries ({@code estimate}/{@code count}) are always answerable and
 * carry the sketch's error bound; membership in this set never does.
 */
public final class BoundedCandidateSet {

    /** Candidate: an item and the number of updates it received while retained. */
    public static final class Candidate {
        public final String item;
        public long observedCount;

        Candidate(String item) {
            this.item = item;
            this.observedCount = 0;
        }
    }

    private final int capacity;
    private final CountMinSketch sketch;
    // Insertion order is retained so tie-breaking beyond the comparator stays stable.
    private final LinkedHashMap<String, Candidate> candidates = new LinkedHashMap<>();

    public BoundedCandidateSet(int capacity, CountMinSketch sketch) {
        if (capacity <= 0) {
            throw new IllegalArgumentException("capacity must be positive");
        }
        this.capacity = capacity;
        this.sketch = sketch;
    }

    public int capacity() {
        return capacity;
    }

    public synchronized int size() {
        return candidates.size();
    }

    public synchronized boolean contains(String item) {
        return candidates.containsKey(item);
    }

    /**
     * Observe one update of {@code item}.
     *
     * @return true if the item is retained after the update, false if it was
     *     evicted (or had never been admitted).
     */
    public synchronized boolean observe(String item) {
        Candidate c = candidates.get(item);
        if (c != null) {
            c.observedCount++;
            return true;
        }
        if (candidates.size() < capacity) {
            Candidate fresh = new Candidate(item);
            fresh.observedCount = 1;
            candidates.put(item, fresh);
            return true;
        }
        // Full: evict the lowest sketch estimate (lexicographic tie-break) if (and
        // only if) the newcomer's current estimate is not lower. Note the newcomer
        // has just been counted by the caller's sketch update, so its estimate
        // includes this observation.
        String victim = pickVictim(item);
        if (victim == null) {
            return false;
        }
        candidates.remove(victim);
        Candidate fresh = new Candidate(item);
        fresh.observedCount = 1;
        candidates.put(item, fresh);
        return true;
    }

    /**
     * Lowest-ranked retained candidate, unless the newcomer ranks no higher; in
     * that case the newcomer is rejected and this returns {@code null}.
     */
    private String pickVictim(String newcomer) {
        long newcomerEstimate = sketch.estimate(newcomer);
        String victim = null;
        long victimEstimate = Long.MAX_VALUE;
        for (Map.Entry<String, Candidate> e : candidates.entrySet()) {
            long est = sketch.estimate(e.getKey());
            boolean worse = est < victimEstimate
                    || (est == victimEstimate && (victim == null
                            || e.getKey().compareTo(victim) < 0));
            if (worse) {
                victimEstimate = est;
                victim = e.getKey();
            }
        }
        // Admission policy: admit when the newcomer ranks strictly higher, or on a
        // tie when the newcomer is lexicographically smaller ("smaller id wins the
        // slot") — this makes eviction deterministic and reproducible.
        if (newcomerEstimate < victimEstimate
                || (newcomerEstimate == victimEstimate && newcomer.compareTo(victim) > 0)) {
            return null;
        }
        return victim;
    }

    /**
     * Return up to {@code k} retained candidates ordered by descending sketch
     * estimate (ties: descending observed count, then lexicographic item).
     *
     * <p>This is a projection of the retained candidate set &mdash; it never
     * fabricates items that were evicted, and may omit true top-K items.
     */
    public synchronized List<Map.Entry<String, Long>> topK(int k) {
        if (k <= 0) {
            return new ArrayList<>();
        }
        List<Map.Entry<String, Candidate>> list = new ArrayList<>(candidates.entrySet());
        list.sort(Comparator
                .comparingLong((Map.Entry<String, Candidate> e) -> sketch.estimate(e.getKey())).reversed()
                .thenComparingLong((Map.Entry<String, Candidate> e) -> -e.getValue().observedCount)
                .thenComparing(Map.Entry::getKey));
        List<Map.Entry<String, Long>> out = new ArrayList<>();
        int n = Math.min(k, list.size());
        for (int i = 0; i < n; i++) {
            Map.Entry<String, Candidate> e = list.get(i);
            out.add(new HashMap.SimpleEntry<>(e.getKey(), sketch.estimate(e.getKey())));
        }
        return out;
    }

    /** Snapshot of retained items and their observed-while-retained counts. */
    public synchronized Map<String, Long> candidateCounts() {
        Map<String, Long> out = new LinkedHashMap<>();
        for (Map.Entry<String, Candidate> e : candidates.entrySet()) {
            out.put(e.getKey(), e.getValue().observedCount);
        }
        return out;
    }
}

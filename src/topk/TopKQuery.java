package topk;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * A validated grouped-TopK query.
 *
 * k = 0 is legal and yields an empty result per group.
 * k < 0 is illegal and rejected here.
 * k must not exceed the engine's per-group memory budget.
 */
public final class TopKQuery {

    private final int k;
    private final int shards;
    /** Optional explicit merge order (a permutation of shard indexes); null = natural order. */
    private final List<Integer> mergeOrder;

    public TopKQuery(int k, int shards, List<Integer> mergeOrder, int budget) {
        if (k < 0) {
            throw new IllegalArgumentException("k must be >= 0, got " + k);
        }
        if (k > budget) {
            throw new IllegalArgumentException(
                    "k (" + k + ") exceeds per-group memory budget (" + budget + ")");
        }
        if (shards < 1) {
            throw new IllegalArgumentException("shards must be >= 1, got " + shards);
        }
        if (mergeOrder != null) {
            if (mergeOrder.size() != shards) {
                throw new IllegalArgumentException(
                        "mergeOrder must be a permutation of 0.." + (shards - 1));
            }
            boolean[] seen = new boolean[shards];
            for (int i : mergeOrder) {
                if (i < 0 || i >= shards || seen[i]) {
                    throw new IllegalArgumentException(
                            "mergeOrder must be a permutation of 0.." + (shards - 1));
                }
                seen[i] = true;
            }
        }
        this.k = k;
        this.shards = shards;
        this.mergeOrder = mergeOrder == null ? null : new ArrayList<>(mergeOrder);
    }

    public int k() { return k; }
    public int shards() { return shards; }

    /** Merge order as a list of shard indexes; defaults to 0,1,...,shards-1. */
    public List<Integer> mergeOrder() {
        if (mergeOrder != null) return new ArrayList<>(mergeOrder);
        List<Integer> natural = new ArrayList<>();
        for (int i = 0; i < shards; i++) natural.add(i);
        return natural;
    }

    /** Build from a JSON request body: {"k":3,"shards":4,"mergeOrder":[2,0,1,3]} */
    public static TopKQuery fromJson(Map<String, Object> req, int budget) {
        if (!req.containsKey("k")) {
            throw new IllegalArgumentException("missing required field 'k'");
        }
        long k = Json.getLong(req, "k", -1);
        long shards = Json.getLong(req, "shards", 1);
        if (k > Integer.MAX_VALUE || shards > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("k/shards too large");
        }
        List<Integer> mergeOrder = null;
        Object mo = req.get("mergeOrder");
        if (mo != null) {
            if (!(mo instanceof List)) {
                throw new IllegalArgumentException("mergeOrder must be an array of shard indexes");
            }
            mergeOrder = new ArrayList<>();
            for (Object o : (List<?>) mo) {
                if (!(o instanceof Number)) {
                    throw new IllegalArgumentException("mergeOrder must be an array of shard indexes");
                }
                mergeOrder.add(((Number) o).intValue());
            }
        }
        return new TopKQuery((int) k, (int) shards, mergeOrder, budget);
    }
}

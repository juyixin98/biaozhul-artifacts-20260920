package topk;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Single-machine in-memory grouped-TopK engine.
 *
 * Execution model:
 *   1. rows are split into N shards (deterministic round-robin);
 *   2. each shard produces a mergeable {@link PartialState} — per group the
 *      k best rows under the total order (value DESC, seq ASC);
 *   3. partial states are merged one by one in the requested merge order.
 *
 * Memory budget: per shard and group, at most {@code budget} rows are ever
 * materialized. Groups whose shard-local size fits the budget are ranked by
 * a plain in-memory sort; larger groups stream through a bounded min-heap
 * of size k. Both strategies return exactly the same rows because the rank
 * order is total.
 */
public final class QueryEngine {

    private final DataSet data;
    private final int budget;

    public QueryEngine(DataSet data, int budget) {
        if (budget < 1) throw new IllegalArgumentException("budget must be >= 1");
        this.data = data;
        this.budget = budget;
    }

    public int budget() { return budget; }

    public static final class Result {
        public final Map<String, List<Row>> groups;
        public final ExecutionPlan plan;

        Result(Map<String, List<Row>> groups, ExecutionPlan plan) {
            this.groups = groups;
            this.plan = plan;
        }

        public Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("groupCount", groups.size());
            Map<String, Object> gs = new LinkedHashMap<>();
            for (Map.Entry<String, List<Row>> e : groups.entrySet()) {
                List<Object> rows = new ArrayList<>();
                for (Row r : e.getValue()) rows.add(r.toJson());
                gs.put(e.getKey(), rows);
            }
            m.put("groups", gs);
            return m;
        }
    }

    public Result execute(TopKQuery q) {
        List<List<Row>> shards = data.shard(q.shards());
        ExecutionPlan plan = new ExecutionPlan(q.k(), budget, data.size());

        PartialState[] partials = new PartialState[shards.size()];
        for (int i = 0; i < shards.size(); i++) {
            ExecutionPlan.ShardPlan sp = plan.addShard(i, shards.get(i).size());
            partials[i] = computeShard(shards.get(i), q.k(), sp);
        }

        List<Integer> order = q.mergeOrder();
        plan.setMergeOrder(order);
        PartialState acc = new PartialState(q.k());
        for (int idx : order) {
            acc.mergeFrom(partials[idx]);
        }
        return new Result(acc.groups(), plan);
    }

    /** Baseline for tests: full sort of the whole dataset, no sharding. */
    public Map<String, List<Row>> fullSortBaseline(int k) {
        if (k < 0) throw new IllegalArgumentException("k must be >= 0, got " + k);
        Map<String, List<Row>> byGroup = new HashMap<>();
        for (Row r : data.rows()) {
            byGroup.computeIfAbsent(r.group(), g -> new ArrayList<>()).add(r);
        }
        Map<String, List<Row>> out = new java.util.TreeMap<>();
        for (Map.Entry<String, List<Row>> e : byGroup.entrySet()) {
            List<Row> sorted = e.getValue();
            sorted.sort(Row.RANK_ORDER);
            out.put(e.getKey(), new ArrayList<>(sorted.subList(0, Math.min(k, sorted.size()))));
        }
        return out;
    }

    private PartialState computeShard(List<Row> shard, int k, ExecutionPlan.ShardPlan sp) {
        Map<String, List<Row>> byGroup = new HashMap<>();
        for (Row r : shard) {
            byGroup.computeIfAbsent(r.group(), g -> new ArrayList<>()).add(r);
        }
        PartialState state = new PartialState(k);
        for (Map.Entry<String, List<Row>> e : byGroup.entrySet()) {
            List<Row> groupRows = e.getValue();
            if (k == 0) {
                sp.groupStrategies.put(e.getKey(), "none (k=0)");
                state.add(groupRows.get(0)); // registers the group with an empty list
                continue;
            }
            if (groupRows.size() <= budget) {
                sp.groupStrategies.put(e.getKey(), "in-memory-sort");
                groupRows.sort(Row.RANK_ORDER);
                int limit = Math.min(k, groupRows.size());
                for (int i = 0; i < limit; i++) state.add(groupRows.get(i));
            } else {
                sp.groupStrategies.put(e.getKey(), "bounded-heap");
                for (Row r : heapTopK(groupRows, k)) state.add(r);
            }
        }
        return state;
    }

    /**
     * Streaming top-k with a bounded heap of size k. The heap is ordered by
     * the REVERSE rank order so the worst of the current k rows sits on top
     * and gets evicted when a better row arrives. Memory stays O(k).
     */
    private static List<Row> heapTopK(List<Row> rows, int k) {
        if (k == 0) return List.of();
        PriorityQueue<Row> heap = new PriorityQueue<>(k, Row.RANK_ORDER.reversed());
        for (Row r : rows) {
            if (heap.size() < k) {
                heap.offer(r);
            } else if (Row.RANK_ORDER.compare(r, heap.peek()) < 0) {
                heap.poll();
                heap.offer(r);
            }
        }
        List<Row> out = new ArrayList<>(heap);
        out.sort(Row.RANK_ORDER);
        return out;
    }
}

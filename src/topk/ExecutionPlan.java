package topk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Exportable description of how a query was (or would be) executed:
 * sharding, per-group strategy choices, and the merge order.
 */
public final class ExecutionPlan {

    static final class ShardPlan {
        final int shardIndex;
        final int rowCount;
        /** group -> "in-memory-sort" | "bounded-heap" */
        final Map<String, String> groupStrategies = new LinkedHashMap<>();

        ShardPlan(int shardIndex, int rowCount) {
            this.shardIndex = shardIndex;
            this.rowCount = rowCount;
        }

        Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("shard", shardIndex);
            m.put("rows", rowCount);
            m.put("groupStrategies", new LinkedHashMap<>(groupStrategies));
            return m;
        }
    }

    private final int k;
    private final int budget;
    private final int totalRows;
    private final List<ShardPlan> shards = new ArrayList<>();
    private final List<Integer> mergeOrder = new ArrayList<>();

    public ExecutionPlan(int k, int budget, int totalRows) {
        this.k = k;
        this.budget = budget;
        this.totalRows = totalRows;
    }

    ShardPlan addShard(int index, int rowCount) {
        ShardPlan sp = new ShardPlan(index, rowCount);
        shards.add(sp);
        return sp;
    }

    void setMergeOrder(List<Integer> order) {
        mergeOrder.clear();
        mergeOrder.addAll(order);
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("operator", "grouped-topk");
        m.put("k", k);
        m.put("perGroupBudget", budget);
        m.put("totalRows", totalRows);
        m.put("ordering", "value DESC, seq ASC (total order, deterministic)");
        List<Object> shardArr = new ArrayList<>();
        for (ShardPlan sp : shards) shardArr.add(sp.toJson());
        m.put("shards", shardArr);
        m.put("mergeOrder", new ArrayList<>(mergeOrder));
        m.put("mergeNote", "partial states are associative+commutative; merge order does not affect the result");
        return m;
    }

    public String toJsonString() {
        return Json.write(toJson());
    }
}

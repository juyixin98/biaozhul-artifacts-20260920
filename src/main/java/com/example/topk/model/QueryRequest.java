package com.example.topk.model;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * A grouped TopK query request.
 *
 * <ul>
 *   <li>rows / dataFile: input data, either inline or loaded from a JSON file</li>
 *   <li>k: number of rows kept per group; must be {@code >= 0} (0 is legal, negative is not)</li>
 *   <li>order: "desc" (default, largest first) or "asc"</li>
 *   <li>shards + shardStrategy: how rows are partitioned into shards before partial aggregation</li>
 *   <li>mergeOrder: order in which shard states are merged (must not affect the result)</li>
 *   <li>groupBudget: per-group in-memory row cap; groups above it trim to their top-k candidates</li>
 *   <li>exportDir: if set, normalized data, plan and result are exported there as JSON</li>
 * </ul>
 */
public final class QueryRequest {
    public List<RawRow> rows = new ArrayList<>();
    public String dataFile;
    public Integer k;
    public String order = "desc";
    public int shards = 4;
    public String shardStrategy = "roundRobin"; // roundRobin | hash
    public String mergeOrder = "sequential";    // sequential | reverse | pairwise
    public int groupBudget = 10_000;
    public String exportDir;

    public static QueryRequest fromJson(Map<String, Object> m) {
        QueryRequest q = new QueryRequest();
        if (m.containsKey("k")) {
            q.k = asInt(m.get("k"), "k");
        }
        if (m.containsKey("order")) {
            q.order = asString(m.get("order"), "order");
        }
        if (m.containsKey("shards")) {
            q.shards = asInt(m.get("shards"), "shards");
        }
        if (m.containsKey("shardStrategy")) {
            q.shardStrategy = asString(m.get("shardStrategy"), "shardStrategy");
        }
        if (m.containsKey("mergeOrder")) {
            q.mergeOrder = asString(m.get("mergeOrder"), "mergeOrder");
        }
        if (m.containsKey("groupBudget")) {
            q.groupBudget = asInt(m.get("groupBudget"), "groupBudget");
        }
        if (m.containsKey("exportDir")) {
            q.exportDir = asString(m.get("exportDir"), "exportDir");
        }
        if (m.containsKey("dataFile")) {
            q.dataFile = asString(m.get("dataFile"), "dataFile");
        }
        if (m.containsKey("data")) {
            Object data = m.get("data");
            if (!(data instanceof List<?> list)) {
                throw new IllegalArgumentException("'data' must be an array of row objects");
            }
            List<RawRow> rows = new ArrayList<>();
            int i = 0;
            for (Object o : list) {
                if (!(o instanceof Map<?, ?> rm)) {
                    throw new IllegalArgumentException("data[" + i + "] must be an object");
                }
                if (!(rm.get("group") instanceof String g)) {
                    throw new IllegalArgumentException("data[" + i + "].group must be a string");
                }
                if (!(rm.get("value") instanceof Number v) || v.doubleValue() != v.longValue()) {
                    throw new IllegalArgumentException("data[" + i + "].value must be an integer");
                }
                Long seq = null;
                if (rm.containsKey("seq")) {
                    Object so = rm.get("seq");
                    if (!(so instanceof Number sn) || sn.doubleValue() != sn.longValue()) {
                        throw new IllegalArgumentException("data[" + i + "].seq must be an integer");
                    }
                    seq = sn.longValue();
                }
                rows.add(new RawRow(g, v.longValue(), seq));
                i++;
            }
            q.rows = rows;
        }
        return q;
    }

    private static int asInt(Object o, String field) {
        if (!(o instanceof Number n) || n.doubleValue() != n.longValue()) {
            throw new IllegalArgumentException("'" + field + "' must be an integer");
        }
        return n.intValue();
    }

    private static String asString(Object o, String field) {
        if (!(o instanceof String s)) {
            throw new IllegalArgumentException("'" + field + "' must be a string");
        }
        return s;
    }
}

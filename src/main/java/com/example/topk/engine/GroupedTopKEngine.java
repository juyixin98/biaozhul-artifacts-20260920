package com.example.topk.engine;

import com.example.topk.json.Json;
import com.example.topk.model.QueryRequest;
import com.example.topk.model.RawRow;
import com.example.topk.model.Row;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Single-machine, in-memory grouped TopK query engine.
 *
 * Pipeline (mirrored in the exported execution plan):
 * <ol>
 *   <li>ingest + normalize: rows are assigned stable unique sequence numbers</li>
 *   <li>shard: rows are partitioned into N shards</li>
 *   <li>partial aggregate: each shard builds a {@link TopKState} per group</li>
 *   <li>merge: per-group states are merged in the requested order
 *       (result is identical for every order — see {@link RowOrdering})</li>
 *   <li>finalize: groups sorted by name, rows in total order</li>
 * </ol>
 */
public final class GroupedTopKEngine {

    public QueryResult execute(QueryRequest req) {
        validate(req);
        RowOrdering ordering = RowOrdering.of(req.order);

        List<RawRow> input = loadRows(req);
        List<Row> data = normalize(input);

        List<List<Row>> shards = shard(data, req.shards, req.shardStrategy);

        // Per-shard partial states.
        List<Map<String, TopKState>> partials = new ArrayList<>();
        List<Integer> partialGroupCounts = new ArrayList<>();
        List<Integer> partialRetained = new ArrayList<>();
        for (List<Row> shard : shards) {
            Map<String, TopKState> m = new LinkedHashMap<>();
            for (Row r : shard) {
                m.computeIfAbsent(r.group(), g -> new TopKState(req.k, req.groupBudget, ordering)).add(r);
            }
            partials.add(m);
            partialGroupCounts.add(m.size());
            partialRetained.add(m.values().stream().mapToInt(TopKState::retainedRows).sum());
        }

        // Merge per the requested order.
        Map<String, TopKState> merged = merge(partials, req.k, req.groupBudget, ordering, req.mergeOrder);

        // Finalize: groups ordered by name, rows in total order.
        List<GroupResult> groups = new ArrayList<>();
        List<String> boundedGroups = new ArrayList<>();
        for (Map.Entry<String, TopKState> e : new TreeMap<>(merged).entrySet()) {
            groups.add(new GroupResult(e.getKey(), e.getValue().topK()));
            if (e.getValue().bounded()) boundedGroups.add(e.getKey());
        }

        Map<String, Object> plan = buildPlan(
                req, data.size(), shards, partialGroupCounts, partialRetained, boundedGroups);

        QueryResult result = new QueryResult(groups, data, plan);

        if (req.exportDir != null) {
            export(req.exportDir, data, plan, result);
        }
        return result;
    }

    // ---------------------------------------------------------------- validation

    static void validate(QueryRequest req) {
        if (req.k == null) {
            throw new IllegalArgumentException("'k' is required");
        }
        if (req.k < 0) {
            throw new IllegalArgumentException("'k' must be >= 0, got " + req.k);
        }
        if (req.shards < 1) {
            throw new IllegalArgumentException("'shards' must be >= 1, got " + req.shards);
        }
        if (req.groupBudget < 0) {
            throw new IllegalArgumentException("'groupBudget' must be >= 0, got " + req.groupBudget);
        }
        if (req.groupBudget < req.k) {
            throw new IllegalArgumentException(
                    "'groupBudget' (" + req.groupBudget + ") must be >= 'k' (" + req.k + ")");
        }
        if (!"roundRobin".equals(req.shardStrategy) && !"hash".equals(req.shardStrategy)) {
            throw new IllegalArgumentException(
                    "'shardStrategy' must be 'roundRobin' or 'hash', got: " + req.shardStrategy);
        }
        if (!"sequential".equals(req.mergeOrder) && !"reverse".equals(req.mergeOrder)
                && !"pairwise".equals(req.mergeOrder)) {
            throw new IllegalArgumentException(
                    "'mergeOrder' must be 'sequential', 'reverse' or 'pairwise', got: " + req.mergeOrder);
        }
        if (req.rows != null && !req.rows.isEmpty() && req.dataFile != null) {
            throw new IllegalArgumentException("provide either inline 'data' or 'dataFile', not both");
        }
    }

    // ---------------------------------------------------------------- ingest

    private static List<RawRow> loadRows(QueryRequest req) {
        if (req.dataFile == null) {
            return req.rows;
        }
        try {
            String text = Files.readString(Path.of(req.dataFile), StandardCharsets.UTF_8);
            Object parsed = Json.parse(text);
            Object rowsNode;
            if (parsed instanceof List<?> list) {
                rowsNode = list;
            } else if (parsed instanceof Map<?, ?> m && m.get("data") instanceof List<?> list) {
                rowsNode = list;
            } else {
                throw new IllegalArgumentException(
                        "dataFile must contain a JSON array or an object with a 'data' array");
            }
            QueryRequest fileReq = QueryRequest.fromJson(Map.of("data", rowsNode));
            return fileReq.rows;
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalArgumentException("cannot read dataFile '" + req.dataFile + "': " + e.getMessage());
        }
    }

    /** Assign/validate stable unique sequence numbers. */
    public static List<Row> normalize(List<RawRow> input) {
        boolean anySeq = input.stream().anyMatch(r -> r.seq() != null);
        boolean allSeq = input.stream().allMatch(r -> r.seq() != null);
        if (anySeq && !allSeq) {
            throw new IllegalArgumentException("either every row must provide 'seq', or none of them");
        }
        List<Row> rows = new ArrayList<>(input.size());
        if (allSeq) {
            java.util.HashSet<Long> seen = new java.util.HashSet<>();
            int i = 0;
            for (RawRow r : input) {
                if (!seen.add(r.seq())) {
                    throw new IllegalArgumentException("duplicate seq " + r.seq() + " at row " + i);
                }
                rows.add(new Row(r.group(), r.value(), r.seq()));
                i++;
            }
        } else {
            for (int i = 0; i < input.size(); i++) {
                RawRow r = input.get(i);
                rows.add(new Row(r.group(), r.value(), i));
            }
        }
        return rows;
    }

    // ---------------------------------------------------------------- sharding

    static List<List<Row>> shard(List<Row> data, int shardCount, String strategy) {
        List<List<Row>> shards = new ArrayList<>(shardCount);
        for (int i = 0; i < shardCount; i++) shards.add(new ArrayList<>());
        int i = 0;
        for (Row r : data) {
            int idx = switch (strategy) {
                case "hash" -> Math.floorMod(r.group().hashCode(), shardCount);
                default -> i % shardCount; // roundRobin spreads every group across shards
            };
            shards.get(idx).add(r);
            i++;
        }
        return shards;
    }

    // ---------------------------------------------------------------- merge

    static Map<String, TopKState> merge(List<Map<String, TopKState>> partials,
                                        int k, int budget, RowOrdering ordering, String mergeOrder) {
        return switch (mergeOrder) {
            case "sequential" -> fold(partials, 0, partials.size(), k, budget, ordering);
            case "reverse" -> fold(partials, partials.size() - 1, -1, k, budget, ordering);
            case "pairwise" -> pairwise(partials, k, budget, ordering);
            default -> throw new IllegalStateException("unreachable");
        };
    }

    /** Fold shard maps into an accumulator, walking indices from {@code start} toward {@code end}. */
    private static Map<String, TopKState> fold(List<Map<String, TopKState>> partials,
                                               int start, int end, int k, int budget, RowOrdering ordering) {
        int step = start <= end ? 1 : -1;
        Map<String, TopKState> acc = new LinkedHashMap<>();
        for (int i = start; i != end; i += step) {
            mergeInto(acc, partials.get(i), k, budget, ordering);
        }
        return acc;
    }

    /** Tournament reduction: repeatedly merge adjacent shard maps. */
    private static Map<String, TopKState> pairwise(List<Map<String, TopKState>> partials,
                                                   int k, int budget, RowOrdering ordering) {
        List<Map<String, TopKState>> work = new ArrayList<>(partials);
        while (work.size() > 1) {
            List<Map<String, TopKState>> next = new ArrayList<>((work.size() + 1) / 2);
            for (int i = 0; i < work.size(); i += 2) {
                if (i + 1 < work.size()) {
                    Map<String, TopKState> combined = new LinkedHashMap<>();
                    mergeInto(combined, work.get(i), k, budget, ordering);
                    mergeInto(combined, work.get(i + 1), k, budget, ordering);
                    next.add(combined);
                } else {
                    next.add(work.get(i)); // odd one out advances unchanged
                }
            }
            work = next;
        }
        return work.isEmpty() ? new LinkedHashMap<>() : work.get(0);
    }

    private static void mergeInto(Map<String, TopKState> acc, Map<String, TopKState> part,
                                  int k, int budget, RowOrdering ordering) {
        for (Map.Entry<String, TopKState> e : part.entrySet()) {
            acc.computeIfAbsent(e.getKey(), g -> new TopKState(k, budget, ordering))
               .mergeInPlace(e.getValue());
        }
    }

    // ---------------------------------------------------------------- plan / export

    private static Map<String, Object> buildPlan(
            QueryRequest req, int rowCount, List<List<Row>> shards,
            List<Integer> partialGroupCounts, List<Integer> partialRetained,
            List<String> boundedGroups) {
        List<Integer> shardSizes = shards.stream().map(List::size).toList();
        List<Map<String, Object>> phases = new ArrayList<>();
        phases.add(Map.of("phase", "ingest",
                "description", "normalize rows and assign stable unique seq (final tie-breaker)"));
        phases.add(Map.of("phase", "shard",
                "description", "partition rows into independent shards",
                "count", req.shards,
                "strategy", req.shardStrategy,
                "shardSizes", shardSizes));
        phases.add(Map.of("phase", "partial-topk",
                "description", "each shard keeps per-group candidates, trimmed to top-k past the budget",
                "groupsPerShard", partialGroupCounts,
                "retainedRowsPerShard", partialRetained,
                "groupBudget", req.groupBudget));
        phases.add(Map.of("phase", "merge",
                "description", "merge per-group intermediate states; total order makes this order-independent",
                "mergeOrder", req.mergeOrder));
        phases.add(Map.of("phase", "finalize",
                "description", "sort groups by name and emit rows in total order"));

        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("engine", "grouped-topk");
        plan.put("k", req.k);
        plan.put("order", req.order);
        plan.put("ordering", "desc".equals(req.order) ? "value DESC, seq ASC" : "value ASC, seq ASC");
        plan.put("rowCount", rowCount);
        plan.put("groupBudget", req.groupBudget);
        plan.put("boundedGroups", boundedGroups);
        plan.put("phases", phases);
        return plan;
    }

    /** Serialize a result group/rows to plain JSON structures. Shared by HTTP response and export. */
    public static Map<String, Object> resultToJson(QueryResult result) {
        List<Map<String, Object>> groups = new ArrayList<>();
        for (GroupResult g : result.groups()) {
            groups.add(Map.of(
                    "group", g.group(),
                    "rows", g.rows().stream().map(GroupedTopKEngine::rowToJson).toList()));
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("groupCount", result.groups().size());
        out.put("groups", groups);
        out.put("plan", result.plan());
        return out;
    }

    private static Map<String, Object> rowToJson(Row r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("group", r.group());
        m.put("value", r.value());
        m.put("seq", r.seq());
        return m;
    }

    public static List<Map<String, Object>> dataToJson(List<Row> data) {
        return data.stream().map(GroupedTopKEngine::rowToJson).toList();
    }

    private static void export(String exportDir, List<Row> data,
                               Map<String, Object> plan, QueryResult result) {
        try {
            Path dir = Path.of(exportDir);
            Files.createDirectories(dir);
            Files.writeString(dir.resolve("data.json"), Json.write(dataToJson(data)) + "\n");
            Files.writeString(dir.resolve("plan.json"), Json.write(plan) + "\n");
            Files.writeString(dir.resolve("result.json"), Json.write(resultToJson(result)) + "\n");
        } catch (Exception e) {
            throw new IllegalArgumentException("export to '" + exportDir + "' failed: " + e.getMessage());
        }
    }
}

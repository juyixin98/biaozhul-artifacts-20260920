package com.example.eventorder.engine;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Counts and enumerates linear extensions of the dependency DAG.
 *
 * <p>Counting uses subset DP and is exact for up to {@value #MAX_EXACT_COUNT_NODES}
 * events; beyond that the count is reported as -1 (not computed). Enumeration is
 * capped by the request's {@code maxEnumeratedOrders}.
 */
public final class LinearExtensions {

    /** Subset DP needs 2^n longs; 20 keeps it at 8 MiB. Larger graphs report -1. */
    static final int MAX_EXACT_COUNT_NODES = 20;

    private LinearExtensions() {
    }

    /**
     * Exact number of topological orders, or -1 when the graph is too large for
     * exact counting. Caller must guarantee acyclicity.
     */
    public static long count(DependencyGraph graph) {
        List<String> ids = graph.nodeIds();
        int n = ids.size();
        if (n > MAX_EXACT_COUNT_NODES) {
            return -1;
        }
        Map<String, Integer> index = new TreeMap<>();
        for (int i = 0; i < n; i++) {
            index.put(ids.get(i), i);
        }
        int[] prerequisiteMask = new int[n];
        for (int i = 0; i < n; i++) {
            for (String succ : graph.successorsOf(ids.get(i))) {
                prerequisiteMask[index.get(succ)] |= 1 << i;
            }
        }
        long[] ways = new long[1 << n];
        ways[0] = 1;
        for (int mask = 0; mask < ways.length; mask++) {
            if (ways[mask] == 0) {
                continue;
            }
            for (int i = 0; i < n; i++) {
                if ((mask & (1 << i)) == 0 && (prerequisiteMask[i] & ~mask) == 0) {
                    ways[mask | (1 << i)] += ways[mask];
                }
            }
        }
        return ways[ways.length - 1];
    }

    /**
     * Up to {@code limit} linear extensions in lexicographic order (viewing each
     * order as a sequence of ids). Caller must guarantee acyclicity.
     */
    public static List<List<String>> enumerate(DependencyGraph graph, int limit) {
        List<List<String>> result = new ArrayList<>();
        if (limit <= 0) {
            return result;
        }
        Map<String, Integer> indegree = new TreeMap<>(graph.indegrees());
        List<String> current = new ArrayList<>();
        enumerateRec(graph, indegree, current, result, limit);
        return result;
    }

    private static void enumerateRec(DependencyGraph graph, Map<String, Integer> indegree,
                                     List<String> current, List<List<String>> result, int limit) {
        if (result.size() >= limit) {
            return;
        }
        if (current.size() == indegree.size()) {
            result.add(List.copyOf(current));
            return;
        }
        for (String id : graph.nodeIds()) {
            if (indegree.get(id) != 0) {
                continue;
            }
            indegree.put(id, -1); // mark placed
            for (String next : graph.successorsOf(id)) {
                indegree.merge(next, -1, Integer::sum);
            }
            current.add(id);
            enumerateRec(graph, indegree, current, result, limit);
            current.remove(current.size() - 1);
            for (String next : graph.successorsOf(id)) {
                indegree.merge(next, 1, Integer::sum);
            }
            indegree.put(id, 0);
            if (result.size() >= limit) {
                return;
            }
        }
    }
}

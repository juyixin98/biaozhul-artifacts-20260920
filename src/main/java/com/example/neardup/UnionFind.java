package com.example.neardup;

import java.util.Map;

/** Union-Find with path compression and union by rank. */
public final class UnionFind {

    private final int[] parent;
    private final int[] rank;

    public UnionFind(int size) {
        parent = new int[size];
        rank = new int[size];
        for (int i = 0; i < size; i++) {
            parent[i] = i;
        }
    }

    public int find(int x) {
        int root = x;
        while (parent[root] != root) {
            root = parent[root];
        }
        while (parent[x] != root) {
            int next = parent[x];
            parent[x] = root;
            x = next;
        }
        return root;
    }

    public void union(int a, int b) {
        int ra = find(a);
        int rb = find(b);
        if (ra == rb) {
            return;
        }
        if (rank[ra] < rank[rb]) {
            int t = ra;
            ra = rb;
            rb = t;
        }
        parent[rb] = ra;
        if (rank[ra] == rank[rb]) {
            rank[ra]++;
        }
    }

    /** Groups indexes by component root, in first-seen order. */
    public Map<Integer, java.util.List<Integer>> components() {
        Map<Integer, java.util.List<Integer>> groups = new java.util.LinkedHashMap<>();
        for (int i = 0; i < parent.length; i++) {
            groups.computeIfAbsent(find(i), k -> new java.util.ArrayList<>()).add(i);
        }
        return groups;
    }
}

package neardup.core;

/** Classic disjoint-set / union-find with path compression and union by size. */
public final class UnionFind {

    private final int[] parent;
    private final int[] size;
    private int components;

    public UnionFind(int n) {
        this.parent = new int[n];
        this.size = new int[n];
        for (int i = 0; i < n; i++) {
            parent[i] = i;
            size[i] = 1;
        }
        this.components = n;
    }

    public int find(int x) {
        int root = x;
        while (parent[root] != root) {
            root = parent[root];
        }
        while (parent[x] != x) {
            int next = parent[x];
            parent[x] = root;
            x = next;
        }
        return root;
    }

    public boolean union(int x, int y) {
        int rx = find(x);
        int ry = find(y);
        if (rx == ry) {
            return false;
        }
        if (size[rx] < size[ry]) {
            int t = rx;
            rx = ry;
            ry = t;
        }
        parent[ry] = rx;
        size[rx] += size[ry];
        components--;
        return true;
    }

    public int components() {
        return components;
    }
}

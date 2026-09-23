package joinorder;

import java.util.ArrayList;
import java.util.List;

/**
 * 连接图中的一条无向边：两张表之间的等值连接条件集合。
 * 两表之间至多一条边（重复声明合并到同一条边）。
 */
public final class Edge {
    public final int tableA;
    public final int tableB;
    public final List<EqPredicate> predicates = new ArrayList<>();

    public Edge(int tableA, int tableB) {
        // 规范化为 tableA < tableB，保证去重与确定性
        this.tableA = Math.min(tableA, tableB);
        this.tableB = Math.max(tableA, tableB);
    }

    public boolean connects(int x, int y) {
        return (tableA == x && tableB == y) || (tableA == y && tableB == x);
    }

    /** 边是否正好一端在 maskA、一端在 maskB（两者不相交）。 */
    public boolean crossesMasks(int maskA, int maskB) {
        int ab = 1 << tableA, bb = 1 << tableB;
        return ((maskA & ab) != 0 && (maskB & bb) != 0)
                || ((maskB & ab) != 0 && (maskA & bb) != 0);
    }

    @Override public String toString() { return tableA + "--" + tableB; }
}

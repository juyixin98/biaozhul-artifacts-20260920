package joinopt;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 等值连接图的连通分量（并查集），供 DP 与穷举器共享“合法分区”判定。
 *
 * 合法分区规则（对应 System R 动态规划中“只枚举连通子问题”的经典约束）：
 * <ul>
 *   <li>S 是连通子集时，只接受<b>有跨分区等值谓词</b>的分区（不允许凭空造笛卡尔积）；</li>
 *   <li>S 跨越多个连通分量时，任何分区都没有跨边，且两个分区必须各自是
 *       若干完整分量的并（分量绝不能被拆开）。</li>
 * </ul>
 * 这样：连通查询产生纯等值连接计划；断开的连接图只在分量之间出现笛卡尔积节点，
 * 且这些笛卡尔积被代价模型自然推迟到最小中间结果的位置。
 */
public final class Components {

    /** 每个连通分量的表掩码。 */
    public final List<Integer> componentMasks;
    private final int[] componentOfTable;

    public Components(int tableCount, List<JoinPred> preds) {
        int[] parent = new int[tableCount];
        for (int i = 0; i < tableCount; i++) parent[i] = i;
        for (JoinPred p : preds) union(parent, p.leftTable, p.rightTable);

        int[] rootToComp = new int[tableCount];
        Arrays.fill(rootToComp, -1);
        List<Integer> masks = new ArrayList<>();
        int[] compOf = new int[tableCount];
        for (int i = 0; i < tableCount; i++) {
            int root = find(parent, i);
            if (rootToComp[root] < 0) {
                rootToComp[root] = masks.size();
                masks.add(0);
            }
            int c = rootToComp[root];
            compOf[i] = c;
            masks.set(c, masks.get(c) | (1 << i));
        }
        this.componentMasks = masks;
        this.componentOfTable = compOf;
    }

    /** mask 是否恰好由若干完整连通分量构成（单分量也算）。 */
    public boolean isUnionOfComponents(int mask) {
        int covered = 0;
        for (int cMask : componentMasks) {
            if ((mask & cMask) != 0) covered |= cMask;
        }
        return covered == mask;
    }

    /** mask 是否为单个连通分量的子集（落在一个分量内）。 */
    public boolean liesWithinOneComponent(int mask) {
        int c = -1;
        for (int t = 0; t < componentOfTable.length; t++) {
            if (((mask >>> t) & 1) == 1) {
                if (c < 0) c = componentOfTable[t];
                else if (c != componentOfTable[t]) return false;
            }
        }
        return true;
    }

    /**
     * 分区 (leftMask, rightMask) 是否合法。
     * 单连通分量内部 -> 必须有跨边；跨分量 -> 两侧都必须是完整分量的并（此时必无跨边）。
     */
    public boolean legalSplit(int leftMask, int rightMask, boolean hasCrossingPred) {
        if (liesWithinOneComponent(leftMask | rightMask)) {
            return hasCrossingPred;
        }
        return !hasCrossingPred
                && isUnionOfComponents(leftMask)
                && isUnionOfComponents(rightMask);
    }

    public int size() {
        return componentMasks.size();
    }

    private static int find(int[] parent, int x) {
        while (parent[x] != x) {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        return x;
    }

    private static void union(int[] parent, int a, int b) {
        int ra = find(parent, a), rb = find(parent, b);
        if (ra != rb) parent[ra] = rb;
    }
}

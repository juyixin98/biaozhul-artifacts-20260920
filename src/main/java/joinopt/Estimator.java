package joinopt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 基数（行数）与不同值个数估计器。
 *
 * <h2>估计模型</h2>
 * 对表子集 S，设 E 为“两端表都属于 S”的等值连接谓词集合（图边），则：
 * <pre>
 *   |⋈ S| = Π_{t ∈ S} |t|  ×  Π_{e=(t.a = u.b) ∈ E} 1 / max(NDV(t.a), NDV(u.b))
 *
 *   NDV(S, t.a) = min( NDV(t.a), |⋈ S| )
 * </pre>
 * 这是数据库教科书（Selinger 风格的等连接均匀假设）的经典估计：
 * <ul>
 *   <li>每条连接边的选择率为 1/max(两侧 NDV)，假设各列值均匀分布且谓词间独立；</li>
 *   <li>计算<b>只使用基表统计</b>，因此同一个子集 S 的估计结果与连接树形状、
 *       连接先后顺序完全无关（顺序不变性），便于穷举校验；</li>
 *   <li>当谓词两端 NDV 不一致（如外键 NDV 小于主键 NDV）时取 max，得到的估计
 *       不会超过较小一侧的输入行数，是保守且自洽的；</li>
 *   <li>任何一张基表为空（行数 0）时，含它的所有连接估计行数为 0。</li>
 * </ul>
 *
 * 注意：均匀分布假设在数据倾斜时会严重失准，见 samples/skew3 与实验记录（README）。
 */
public final class Estimator {

    private final List<Table> tables;
    private final List<JoinPred> preds;
    /** 每张表（基表）参与的谓词索引。 */
    private final List<List<Integer>> edgesByTable;

    public Estimator(List<Table> tables, List<JoinPred> preds) {
        this.tables = tables;
        this.preds = preds;
        this.edgesByTable = new ArrayList<>();
        for (int t = 0; t < tables.size(); t++) edgesByTable.add(new ArrayList<>());
        for (int i = 0; i < preds.size(); i++) {
            JoinPred p = preds.get(i);
            edgesByTable.get(p.leftTable).add(i);
            edgesByTable.get(p.rightTable).add(i);
        }
    }

    /** 计算子集 mask（位掩码，第 i 位对应 tables.get(i)）的统计。 */
    public Stats estimate(int mask) {
        if (mask == 0) return new Stats(0, new LinkedHashMap<>());

        double rows = 1.0;
        boolean hasRows = false;
        for (int t = 0; t < tables.size(); t++) {
            if (bit(mask, t)) {
                long r = tables.get(t).rowCount;
                if (r == 0) {
                    return emptyStats(mask);
                }
                rows *= r;
                hasRows = true;
            }
        }
        if (!hasRows) return emptyStats(mask);

        // 只乘子集内部的边；跨子集的边留给上层连接节点
        for (int i = 0; i < preds.size(); i++) {
            JoinPred p = preds.get(i);
            if (bit(mask, p.leftTable) && bit(mask, p.rightTable)) {
                long ndvL = baseNdv(p.leftTable, p.leftColumn);
                long ndvR = baseNdv(p.rightTable, p.rightColumn);
                long denom = Math.max(ndvL, ndvR);
                rows /= (denom == 0 ? 1 : denom);
            }
        }
        if (rows < 0) rows = 0; // 数值保护

        long rowCap = Math.round(Math.ceil(rows));
        Map<String, Long> ndv = new LinkedHashMap<>();
        for (int t = 0; t < tables.size(); t++) {
            if (bit(mask, t)) {
                Table tab = tables.get(t);
                for (String col : tab.columns) {
                    long base = tab.ndv.get(col);
                    ndv.put(tab.qualified(tab.columns.indexOf(col)), Math.min(base, rowCap));
                }
            }
        }
        return new Stats(rows, ndv);
    }

    private Stats emptyStats(int mask) {
        Map<String, Long> ndv = new LinkedHashMap<>();
        for (int t = 0; t < tables.size(); t++) {
            if (bit(mask, t)) {
                Table tab = tables.get(t);
                for (int i = 0; i < tab.columns.size(); i++) {
                    ndv.put(tab.qualified(i), 0L);
                }
            }
        }
        return new Stats(0, ndv);
    }

    /**
     * 两个子集 L、R 做一次连接后的估计行数。
     * crossing 为跨越 L、R 的等值谓词；为空表示笛卡尔积。
     */
    public double joinRows(Stats left, Stats right, int leftMask, int rightMask, List<JoinPred> crossing) {
        double r = left.rows * right.rows;
        for (JoinPred p : crossing) {
            long nl = baseNdv(p.leftTable, p.leftColumn);
            long nr = baseNdv(p.rightTable, p.rightColumn);
            long denom = Math.max(nl, nr);
            if (denom > 0) r /= denom;
        }
        return r;
    }

    private long baseNdv(int tableIdx, String column) {
        Table t = tables.get(tableIdx);
        Long d = t.ndv.get(column);
        if (d == null) {
            throw new IllegalArgumentException("谓词引用的列不存在: " + t.name + "." + column);
        }
        return d;
    }

    /** 找出跨越两个子集的谓词（一端在 L、一端在 R）。 */
    public List<JoinPred> crossingPreds(int leftMask, int rightMask) {
        List<JoinPred> out = new ArrayList<>();
        for (JoinPred p : preds) {
            boolean linL = bit(leftMask, p.leftTable);
            boolean rinR = bit(rightMask, p.rightTable);
            boolean linR = bit(rightMask, p.leftTable);
            boolean rinL = bit(leftMask, p.rightTable);
            if ((linL && rinR) || (linR && rinL)) out.add(p);
        }
        return out;
    }

    private static boolean bit(int mask, int i) {
        return ((mask >>> i) & 1) == 1;
    }
}

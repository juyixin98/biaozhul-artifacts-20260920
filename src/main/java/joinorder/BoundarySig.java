package joinorder;

import java.util.List;

/**
 * 子计划的“边界签名”：输出行数 + 各边界列 NDV。
 * 两个签名相同的子计划在其之上的任何连接中表现完全一致，只保留代价最小者即可，
 * 不改变最优值（边界列定义见 {@link Model#boundaryColumns(int)}）。
 */
public final class BoundarySig {
    private final double rows;
    private final double[] ndvs;
    private final int hash;

    public BoundarySig(Stats s, List<String> boundaryOrdered) {
        this.rows = s.rowCount;
        this.ndvs = new double[boundaryOrdered.size()];
        for (int i = 0; i < boundaryOrdered.size(); i++) {
            ndvs[i] = s.ndv.getOrDefault(boundaryOrdered.get(i), s.rowCount);
        }
        int h = Double.hashCode(rows);
        for (double v : ndvs) h = 31 * h + Double.hashCode(v);
        this.hash = h;
    }

    @Override public boolean equals(Object o) {
        if (!(o instanceof BoundarySig)) return false;
        BoundarySig b = (BoundarySig) o;
        if (Math.abs(rows - b.rows) > 1e-9 || ndvs.length != b.ndvs.length) return false;
        for (int i = 0; i < ndvs.length; i++) {
            if (Math.abs(ndvs[i] - b.ndvs[i]) > 1e-9) return false;
        }
        return true;
    }
    @Override public int hashCode() { return hash; }
}

package joinorder;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 关系（基表或连接中间结果）的统计信息：
 *   rowCount     行数估计
 *   ndv          每个列的不同值数（distinct value count）
 *
 * 这是本项目代价模型的全部依据：连接基数 = 两侧行数之积 / 各等值列 max(ndv) 的乘积。
 */
public final class Stats {
    public double rowCount;
    public final Map<String, Double> ndv = new LinkedHashMap<>();

    public Stats() {}

    public Stats(double rowCount) {
        this.rowCount = rowCount;
    }

    public double getNdv(String canonicalColumn) {
        Double d = ndv.get(canonicalColumn);
        if (d != null) return d;
        return rowCount; // 无 NDV 时退化为主键式假设：每行一个不同值
    }

    public Stats copy() {
        Stats s = new Stats(rowCount);
        s.ndv.putAll(this.ndv);
        return s;
    }
}

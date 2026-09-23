package joinopt;

import java.util.LinkedHashMap;
import java.util.Map;

/** 一个关系（基表或若干表连接后的中间结果）的统计快照：估计行数与每列 NDV。 */
public final class Stats {

    public final double rows;
    /** 键为全限定列名 "表.列"。 */
    public final Map<String, Long> ndv;

    public Stats(double rows, Map<String, Long> ndv) {
        this.rows = rows;
        this.ndv = ndv;
    }

    public long roundedRows() {
        return Math.round(rows);
    }
}

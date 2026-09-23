package joinopt;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 内存中的关系：全限定列名 + 物化数据行。
 * 这是单机查询引擎在计划执行期间传递的中间结果表示。
 */
public final class Rel {

    public final List<String> columns;
    public final List<List<Object>> rows;
    private final Map<String, Integer> index;

    public Rel(List<String> columns, List<List<Object>> rows) {
        this.columns = columns;
        this.rows = rows;
        this.index = new HashMap<>();
        for (int i = 0; i < columns.size(); i++) index.put(columns.get(i), i);
    }

    public int indexOf(String qualifiedColumn) {
        Integer i = index.get(qualifiedColumn);
        if (i == null) throw new IllegalArgumentException("中间结果中不存在列 " + qualifiedColumn);
        return i;
    }

    /** 取一行在若干列上的规范化复合键（列顺序需在连接两侧保持一致）。 */
    public static String keyOf(List<Object> row, int[] idxs) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < idxs.length; i++) {
            if (i > 0) sb.append('|');
            Object v = row.get(idxs[i]);
            sb.append(v == null ? "#null" : Table.canon(v));
        }
        return sb.toString();
    }
}

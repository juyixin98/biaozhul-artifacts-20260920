package joinopt;

import java.util.Map;

/**
 * 等值连接谓词：leftTable.leftColumn = rightTable.rightColumn。
 *
 * JSON 支持两种写法：
 *   数组简写: ["orders","cust_id","customers","id"]
 *   对象写法: {"leftTable":"orders","leftColumn":"cust_id",
 *             "rightTable":"customers","rightColumn":"id"}
 */
public final class JoinPred {

    public final int leftTable;
    public final String leftColumn;
    public final int rightTable;
    public final String rightColumn;

    public JoinPred(int leftTable, String leftColumn, int rightTable, String rightColumn) {
        this.leftTable = leftTable;
        this.leftColumn = leftColumn;
        this.rightTable = rightTable;
        this.rightColumn = rightColumn;
    }

    public static JoinPred fromJson(Object json, Map<String, Integer> tableIndex, String label) {
        int lt, rt;
        String lc, rc;
        if (json instanceof java.util.List) {
            java.util.List<Object> a = Json.asArr(json);
            if (a.size() != 4) {
                throw new IllegalArgumentException("谓词 " + label + " 数组形式必须有 4 个元素"
                        + " [表,列,表,列]");
            }
            lt = resolveTable(String.valueOf(a.get(0)), tableIndex, label);
            lc = String.valueOf(a.get(1));
            rt = resolveTable(String.valueOf(a.get(2)), tableIndex, label);
            rc = String.valueOf(a.get(3));
        } else {
            Map<String, Object> m = Json.asObj(json);
            String ltn = Json.str(m, "leftTable");
            String rtn = Json.str(m, "rightTable");
            lc = Json.str(m, "leftColumn");
            rc = Json.str(m, "rightColumn");
            if (ltn == null || rtn == null || lc == null || rc == null) {
                throw new IllegalArgumentException("谓词 " + label + " 缺少字段（需要 "
                        + "leftTable/leftColumn/rightTable/rightColumn）");
            }
            lt = resolveTable(ltn, tableIndex, label);
            rt = resolveTable(rtn, tableIndex, label);
        }
        if (lt == rt) {
            throw new IllegalArgumentException("谓词 " + label + " 两端必须是不同的表（当前均为表 #" + lt + "）");
        }
        return new JoinPred(lt, lc, rt, rc);
    }

    private static int resolveTable(String name, Map<String, Integer> idx, String label) {
        Integer i = idx.get(name);
        if (i == null) {
            throw new IllegalArgumentException("谓词 " + label + " 引用了不存在的表: " + name);
        }
        return i;
    }

    @Override
    public String toString() {
        return "#" + leftTable + "." + leftColumn + " = #" + rightTable + "." + rightColumn;
    }
}

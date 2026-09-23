package testutil;

import engine.model.ColumnType;
import engine.model.Relation;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** 行内容规范化工具：把输出行按所有列做全序排序，便于跨实现逐行比较。 */
public final class Rows {

    private Rows() {
    }

    public static List<Object[]> canonical(Relation r) {
        List<Object[]> rows = new ArrayList<>();
        for (Relation.Row row : r.rows()) {
            rows.add(row.values.clone());
        }
        rows.sort(rowsComparator());
        return rows;
    }

    /** 全列全序：null 最小，Long 按数值，String 按字典序；逐列决胜。 */
    public static Comparator<Object[]> rowsComparator() {
        return (a, b) -> {
            int n = Math.min(a.length, b.length);
            for (int i = 0; i < n; i++) {
                int c = compareCell(a[i], b[i]);
                if (c != 0) {
                    return c;
                }
            }
            return Integer.compare(a.length, b.length);
        };
    }

    @SuppressWarnings("unchecked")
    public static int compareCell(Object a, Object b) {
        if (a == null && b == null) {
            return 0;
        }
        if (a == null) {
            return -1;
        }
        if (b == null) {
            return 1;
        }
        if (a instanceof Long && b instanceof Long) {
            return Long.compare((Long) a, (Long) b);
        }
        if (a instanceof String && b instanceof String) {
            return ((String) a).compareTo((String) b);
        }
        throw new IllegalArgumentException("无法比较不同类型的单元: " + a + " / " + b);
    }

    public static List<String> typeNames(Relation r) {
        return r.columnTypes().stream().map(ColumnType::name).toList();
    }
}

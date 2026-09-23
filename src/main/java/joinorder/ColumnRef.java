package joinorder;

import java.util.Objects;

/** 列的规范引用：表名 + 列名（形如 orders.cust_id）。 */
public final class ColumnRef {
    public final String table;
    public final String column;
    public final String canonical;

    private ColumnRef(String table, String column) {
        this.table = table;
        this.column = column;
        this.canonical = table + "." + column;
    }

    /** 解析 "t.c" 形式；裸列名 "c" 在有表上下文时由 Model 补全。 */
    public static ColumnRef parse(String text) {
        int idx = text.indexOf('.');
        if (idx <= 0 || idx == text.length() - 1) {
            throw new EngineException("列引用 '" + text + "' 应为 '表名.列名' 形式");
        }
        return new ColumnRef(text.substring(0, idx), text.substring(idx + 1));
    }

    public static ColumnRef of(String table, String column) {
        return new ColumnRef(table, column);
    }

    @Override public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof ColumnRef)) return false;
        return canonical.equals(((ColumnRef) o).canonical);
    }
    @Override public int hashCode() { return Objects.hashCode(canonical); }
    @Override public String toString() { return canonical; }
}

package ppd;

import java.util.ArrayList;
import java.util.List;

/**
 * 输出列描述。
 *
 * @param qualifier 所属表别名（投影计算列可为 null）
 * @param name      列名
 * @param nullable  在当前计划节点的输出里该列是否可能为 NULL
 * @param origin    列来源；null 表示计算列（投影表达式）或无来源
 */
public record Column(String qualifier, String name, boolean nullable, Column origin) {

    public Column(String qualifier, String name, boolean nullable) {
        this(qualifier, name, nullable, null);
    }

    /** 带来源的同位置列。 */
    public Column withOrigin(Column o) {
        return new Column(qualifier, name, nullable, o);
    }

    public Column withNullable(boolean n) {
        return new Column(qualifier, name, n, origin);
    }

    /** 来源终点（穿透多层投影）。 */
    public Column rootOrigin() {
        return origin == null ? this : origin.rootOrigin();
    }

    public boolean sameIdentity(Column c) {
        return java.util.Objects.equals(qualifier, c.qualifier) && name.equals(c.name);
    }

    public String display() {
        return qualifier == null ? name : qualifier + "." + name;
    }

    public static List<String> displayList(List<Column> cols) {
        List<String> out = new ArrayList<>();
        for (Column c : cols) out.add(c.display());
        return out;
    }
}

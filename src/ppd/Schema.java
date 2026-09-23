package ppd;

import java.util.ArrayList;
import java.util.List;

/** 计划节点输出模式：有序列集合，负责把谓词中的列引用解析到具体列。 */
public record Schema(List<Column> columns) {

    public Schema {
        columns = List.copyOf(columns);
    }

    public static Schema of(Column... cols) {
        return new Schema(List.of(cols));
    }

    public int size() { return columns.size(); }

    public Column get(int i) { return columns.get(i); }

    /**
     * 解析列引用：
     * 限定名必须精确匹配；裸列名匹配同名列，多于一个则报歧义错误。
     *
     * @return 匹配到的列；未找到返回 null
     */
    public Column resolve(RefKey ref) {
        List<Column> matches = new ArrayList<>();
        for (Column c : columns) {
            if (!c.name().equals(ref.name())) continue;
            if (ref.qualifier() == null || ref.qualifier().equals(c.qualifier())) {
                matches.add(c);
            }
        }
        if (matches.isEmpty()) return null;
        if (ref.qualifier() == null && matches.size() > 1) {
            List<String> names = Column.displayList(matches);
            throw new EngineException("列名 '" + ref.name() + "' 有歧义，可指向: " + names);
        }
        return matches.get(0);
    }

    public boolean contains(Column c) {
        for (Column x : columns) {
            if (x.sameIdentity(c)) return true;
        }
        return false;
    }

    public int indexOf(Column c) {
        for (int i = 0; i < columns.size(); i++) {
            if (columns.get(i).sameIdentity(c)) return i;
        }
        return -1;
    }

    public List<String> display() {
        return Column.displayList(columns);
    }
}

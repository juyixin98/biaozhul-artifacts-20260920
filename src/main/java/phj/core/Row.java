package phj.core;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** 一行：有序的 Value 列表。 */
public final class Row {

    private final List<Value> values;

    public Row(List<Value> values) {
        this.values = new ArrayList<>(values);
    }

    public static Row of(Object... jsonValues) {
        List<Value> vs = new ArrayList<>(jsonValues.length);
        for (Object o : jsonValues) vs.add(Value.fromJson(o));
        return new Row(vs);
    }

    public Value get(int i) { return values.get(i); }

    public int width() { return values.size(); }

    public List<Value> values() { return Collections.unmodifiableList(values); }

    /**
     * 行相等（数据/多重集语义）：同位置 NULL 视为相等，其余按 Value.equals。
     * 注意与连接键判定（Key.equals，SQL 语义 NULL≠NULL）刻意区分。
     */
    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Row r) || r.values.size() != values.size()) return false;
        for (int i = 0; i < values.size(); i++) {
            Value a = values.get(i);
            Value b = r.values.get(i);
            if (a.isNull() || b.isNull()) {
                if (a.isNull() != b.isNull()) return false;
            } else if (!a.equals(b)) {
                return false;
            }
        }
        return true;
    }

    @Override
    public int hashCode() {
        int h = 1;
        for (Value v : values) {
            // 与 equals 的数据语义对齐：NULL=NULL，LONG/DOUBLE 数值相等哈希相同
            h = 31 * h + v.dataHashCode();
        }
        return h;
    }

    @Override
    public String toString() { return values.toString(); }
}

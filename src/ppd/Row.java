package ppd;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 一行数据：按列下标存放值（null 即 SQL NULL）。 */
public class Row {

    private final Object[] values;

    public Row(int width) {
        this.values = new Object[width];
    }

    public Row(List<Object> values) {
        this.values = values.toArray();
    }

    public static Row nullRow(int width) {
        return new Row(width);
    }

    public int width() { return values.length; }

    public Object get(int i) { return values[i]; }

    public void set(int i, Object v) { values[i] = v; }

    /** 拼接两行（用于连接输出）。 */
    public Row concat(Row other) {
        Row r = new Row(values.length + other.values.length);
        System.arraycopy(values, 0, r.values, 0, values.length);
        System.arraycopy(other.values, 0, r.values, values.length, other.values.length);
        return r;
    }

    /** 按索引投影。 */
    public Row project(int[] indices) {
        Row r = new Row(indices.length);
        for (int i = 0; i < indices.length; i++) r.values[i] = values[indices[i]];
        return r;
    }

    public List<Object> toList() {
        List<Object> out = new ArrayList<>(values.length);
        for (Object v : values) out.add(v);
        return out;
    }

    /** 转成 {限定名.列名: 值} 的有序映射（无别名用裸列名）。 */
    public Map<String, Object> toNamedMap(Schema schema) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < schema.size(); i++) {
            m.put(schema.get(i).display(), values[i]);
        }
        return m;
    }

    /** 结果比较用的不可变行键。 */
    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Row r)) return false;
        if (r.values.length != values.length) return false;
        for (int i = 0; i < values.length; i++) {
            if (!valueEquals(values[i], r.values[i])) return false;
        }
        return true;
    }

    @Override
    public int hashCode() {
        int h = 1;
        for (Object v : values) h = 31 * h + (v == null ? 0 : normalized(v).hashCode());
        return h;
    }

    private static boolean valueEquals(Object a, Object b) {
        if (a == null || b == null) return a == b;
        if (Values.isNumber(a) && Values.isNumber(b)) return Values.numericEqual(a, b);
        return a.equals(b);
    }

    private static Object normalized(Object v) {
        if (v instanceof Integer i) return i.longValue();
        return v;
    }
}

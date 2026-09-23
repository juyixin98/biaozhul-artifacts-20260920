package vecq;

import java.util.LinkedHashMap;
import java.util.Map;

/** 32 位整型列。NULL 行在 data 中的值无意义（占位为 0）。 */
public final class IntColumn extends Column {

    private final int[] data;

    public IntColumn(String name, int[] data, NullBitmap nulls) {
        super(name, data.length, nulls);
        this.data = data;
    }

    public int getInt(int row) {
        return data[row];
    }

    /** 包级原始数组访问（批执行热路径，避免逐行 getter）。调用方必须先用 {@link #isNull(int)} 判空。 */
    int[] raw() {
        return data;
    }

    @Override
    public String typeName() {
        return "int";
    }

    @Override
    public Object[] gather(int[] rows) {
        Object[] out = new Object[rows.length];
        for (int i = 0; i < rows.length; i++) {
            int row = rows[i];
            out[i] = isNull(row) ? null : data[row];
        }
        return out;
    }

    @Override
    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("type", "int");
        Integer[] values = new Integer[size];
        for (int i = 0; i < size; i++) {
            values[i] = isNull(i) ? null : data[i];
        }
        m.put("values", java.util.Arrays.asList(values));
        if (nulls != null) {
            int[] nr = nulls.nullRows();
            java.util.List<Object> l = new java.util.ArrayList<>(nr.length);
            for (int r : nr) l.add(r);
            m.put("nullRows", l);
        }
        return m;
    }
}

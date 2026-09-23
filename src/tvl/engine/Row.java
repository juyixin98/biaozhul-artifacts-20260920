package tvl.engine;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import tvl.evaluator.RowLike;

/**
 * 内存中的一行数据：按列名取值。值为 Long / String / Boolean 或 null（SQL NULL）。
 */
public final class Row implements RowLike {
    private final Map<String, Object> values;

    public Row() {
        this.values = new LinkedHashMap<>();
    }

    public void set(String column, Object value) {
        values.put(column, value);
    }

    @Override
    public Object get(String column) {
        return values.get(column);
    }

    public Set<String> columns() {
        return values.keySet();
    }
}

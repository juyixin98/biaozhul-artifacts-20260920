package vecq;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** UTF-16 字符串列（内部以 String[] 存放，演示用不做字典编码）。 */
public final class StringColumn extends Column {

    private final String[] data;

    public StringColumn(String name, String[] data, NullBitmap nulls) {
        super(name, data.length, nulls);
        this.data = data;
    }

    public String getString(int row) {
        return data[row];
    }

    @Override
    public String typeName() {
        return "string";
    }

    @Override
    public Object[] gather(int[] rows) {
        Object[] out = new Object[rows.length];
        for (int i = 0; i < rows.length; i++) {
            int row = rows[i];
            // NULL 位为权威来源；即使数组槽位里恰好有字符串也必须返回 null
            out[i] = isNull(row) ? null : data[row];
        }
        return out;
    }

    @Override
    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("type", "string");
        List<Object> values = new ArrayList<>(size);
        for (int i = 0; i < size; i++) {
            values.add(isNull(i) ? null : data[i]);
        }
        m.put("values", values);
        if (nulls != null) {
            int[] nr = nulls.nullRows();
            List<Object> l = new ArrayList<>(nr.length);
            for (int r : nr) l.add(r);
            m.put("nullRows", l);
        }
        return m;
    }
}

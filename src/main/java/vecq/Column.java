package vecq;

import java.util.List;
import java.util.Map;

/**
 * 列的抽象基类。值数组与 {@link NullBitmap} 分离：
 * NULL 行在值数组中的槽位无意义（实现中放默认占位值），任何取值都必须先查位图。
 */
public abstract class Column {

    protected final String name;
    protected final int size;
    protected final NullBitmap nulls; // 允许为 null，表示全列非空

    protected Column(String name, int size, NullBitmap nulls) {
        if (name == null || name.isEmpty()) {
            throw new InvalidQueryException("列名不能为空");
        }
        if (size < 0) {
            throw new InvalidQueryException("列长度不能为负");
        }
        if (nulls != null && nulls.size() != size) {
            throw new InvalidQueryException("列 " + name + " 的 NULL 位图长度与列长度不一致");
        }
        this.name = name;
        this.size = size;
        this.nulls = nulls;
    }

    public String name() { return name; }
    public int size() { return size; }
    public NullBitmap nulls() { return nulls; }

    public boolean isNull(int row) {
        return nulls != null && nulls.isNull(row);
    }

    /** "int" 或 "string"。 */
    public abstract String typeName();

    /**
     * 按选择向量（行下标数组，可重复、可稀疏）收集结果。
     * 结果与下标一一对应、保持顺序；命中 NULL 的位置结果为 null。
     * 下标越界由调用方（{@link SelectionVector}）负责，这里不再校验。
     */
    public abstract Object[] gather(int[] rows);

    /** 序列化为 JSON 对象：{name,type,values,nullRows}。 */
    public abstract Map<String, Object> toJson();

    // ---------------- 从 JSON 构造列 ----------------

    static Column fromJson(Map<String, Object> obj) {
        String name = requireString(obj, "name");
        String type = requireString(obj, "type");
        List<Object> values = Json.asList(obj.getOrDefault("values", List.of()));
        int[] nullRows = parseIntRows(obj.get("nullRows"), "nullRows");
        if (nullRows.length == 0 && obj.containsKey("nulls")) {
            // 也允许 "nulls": [行下标...] 的写法
            nullRows = parseIntRows(obj.get("nulls"), "nulls");
        }
        return switch (type) {
            case "int", "integer", "long" -> buildInt(name, values, nullRows);
            case "string", "str" -> buildString(name, values, nullRows);
            default -> throw new InvalidQueryException("列 " + name + " 使用了不支持的类型: " + type);
        };
    }

    private static IntColumn buildInt(String name, List<Object> values, int[] nullRows) {
        int n = values.size();
        int[] data = new int[n];
        int[] explicit = new int[n];
        int ec = 0;
        for (int i = 0; i < n; i++) {
            Object v = values.get(i);
            if (v == null) explicit[ec++] = i;
            else data[i] = toInt(v);
        }
        NullBitmap bm = mergeNulls(name, n, explicit, ec, nullRows);
        return new IntColumn(name, data, bm);
    }

    private static StringColumn buildString(String name, List<Object> values, int[] nullRows) {
        int n = values.size();
        String[] data = new String[n];
        int[] explicit = new int[n];
        int ec = 0;
        for (int i = 0; i < n; i++) {
            Object v = values.get(i);
            if (v == null) explicit[ec++] = i;
            else data[i] = String.valueOf(v);
        }
        NullBitmap bm = mergeNulls(name, n, explicit, ec, nullRows);
        return new StringColumn(name, data, bm);
    }

    /**
     * NULL 有两种表达：values 中写 null，或用 nullRows 位图。
     * 单独使用任意一种都可以；两种并用时声明的行集合必须一致，否则报错。
     */
    private static NullBitmap mergeNulls(String name, int n, int[] explicit, int ec, int[] declaredRows) {
        boolean[] declared = new boolean[n];
        for (int r : declaredRows) {
            if (r < 0 || r >= n) {
                throw new InvalidQueryException("列 " + name + " 的 nullRows 下标越界: " + r);
            }
            declared[r] = true;
        }
        boolean[] explicitSet = new boolean[n];
        for (int i = 0; i < ec; i++) explicitSet[explicit[i]] = true;
        if (ec > 0 && declaredRows.length > 0) {
            // 两种表达并用：集合必须一致
            for (int i = 0; i < n; i++) {
                if (declared[i] != explicitSet[i]) {
                    throw new InvalidQueryException("列 " + name + " 第 " + i
                            + " 行：values 中的 null 与 nullRows 位图声明不一致（可只使用其中一种表达；"
                            + "若两者并用则声明的 NULL 行集合必须完全相同）");
                }
            }
            return NullBitmap.fromNullRows(n, java.util.Arrays.copyOf(explicit, ec));
        }
        if (ec > 0) {
            return NullBitmap.fromNullRows(n, java.util.Arrays.copyOf(explicit, ec));
        }
        if (declaredRows.length > 0) {
            return NullBitmap.fromNullRows(n, declaredRows.clone());
        }
        return null;
    }

    static int[] parseIntRows(Object o, String field) {
        if (o == null) return new int[0];
        List<Object> list = Json.asList(o);
        int[] out = new int[list.size()];
        for (int i = 0; i < list.size(); i++) {
            out[i] = toInt(list.get(i));
        }
        return out;
    }

    static int toInt(Object v) {
        if (v instanceof Number n) {
            long l = n.longValue();
            if (l < Integer.MIN_VALUE || l > Integer.MAX_VALUE) {
                throw new InvalidQueryException("整型列只接受 32 位有符号整数，越界值: " + l);
            }
            return (int) l;
        }
        throw new InvalidQueryException("期望整数，实际为 " + Json.typeName(v) + "（值: " + v + "）");
    }

    static String requireString(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new InvalidQueryException("缺少非空字符串字段 \"" + key + "\"");
        }
        return s;
    }
}

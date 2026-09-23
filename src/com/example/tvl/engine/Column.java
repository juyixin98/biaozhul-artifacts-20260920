package com.example.tvl.engine;

import com.example.tvl.sql.DataType;

import java.util.List;

/**
 * 列式数据。每列持有一个 NULL 位图和按类型分化的原生数组，
 * 向量执行直接遍历这些数组，不做装箱。
 */
public final class Column {

    private final DataType type;
    private final boolean[] nulls;
    private final long[] longs;
    private final double[] doubles;
    private final String[] strings;

    private Column(DataType type, int rowCount) {
        this.type = type;
        this.nulls = new boolean[rowCount];
        this.longs = type == DataType.INTEGER ? new long[rowCount] : null;
        this.doubles = type == DataType.FLOAT ? new double[rowCount] : null;
        this.strings = type == DataType.TEXT ? new String[rowCount] : null;
    }

    public static Column ofLongs(long[] values, boolean[] nulls) {
        Column c = new Column(DataType.INTEGER, values.length);
        System.arraycopy(values, 0, c.longs, 0, values.length);
        System.arraycopy(nulls, 0, c.nulls, 0, values.length);
        return c;
    }

    public static Column ofDoubles(double[] values, boolean[] nulls) {
        Column c = new Column(DataType.FLOAT, values.length);
        System.arraycopy(values, 0, c.doubles, 0, values.length);
        System.arraycopy(nulls, 0, c.nulls, 0, values.length);
        return c;
    }

    public static Column ofStrings(String[] values, boolean[] nulls) {
        Column c = new Column(DataType.TEXT, values.length);
        System.arraycopy(values, 0, c.strings, 0, values.length);
        System.arraycopy(nulls, 0, c.nulls, 0, values.length);
        return c;
    }

    /** 按模式类型从行值（Long/Double/String/null）构造列。 */
    @SuppressWarnings("unchecked")
    static Column fromRows(DataType type, int rowCount, List<?> rows, int colIndex) {
        Column c = new Column(type, rowCount);
        for (int r = 0; r < rowCount; r++) {
            Object raw;
            Object rowObj = rows.get(r);
            if (rowObj instanceof java.util.List<?> row) {
                if (colIndex >= row.size()) {
                    throw new SemanticException("第 " + r + " 行缺少第 " + colIndex + " 列的值");
                }
                raw = row.get(colIndex);
            } else if (rowObj instanceof java.util.Map<?, ?> m) {
                // 调用方负责传入按列名取值的适配结构，这里不应出现
                throw new AssertionError();
            } else {
                throw new SemanticException("rows[" + r + "] 必须是数组");
            }
            c.set(r, raw);
        }
        return c;
    }

    /** 按模式类型从列值数组构造。 */
    static Column fromValues(DataType type, List<?> values) {
        Column c = new Column(type, values.size());
        for (int r = 0; r < values.size(); r++) {
            c.set(r, values.get(r));
        }
        return c;
    }

    private void set(int row, Object raw) {
        if (raw == null) {
            nulls[row] = true;
            return;
        }
        switch (type) {
            case INTEGER -> {
                if (raw instanceof Long l) {
                    longs[row] = l;
                } else if (raw instanceof Integer i) {
                    longs[row] = i.longValue();
                } else if (raw instanceof Double d && d == Math.rint(d)
                        && d >= Long.MIN_VALUE && d <= Long.MAX_VALUE) {
                    // 数值宽化：整数值的 JSON number 进入整型列属于正常数值范畴
                    longs[row] = d.longValue();
                } else if (raw instanceof String) {
                    throw new SemanticException(
                            "INTEGER 列第 " + row + " 行收到字符串（拒绝隐式字符串转数值）");
                } else {
                    throw new SemanticException(
                            "INTEGER 列第 " + row + " 行收到非法值: " + raw);
                }
            }
            case FLOAT -> {
                if (raw instanceof Number n) {
                    doubles[row] = n.doubleValue();
                    if (Double.isNaN(doubles[row]) || Double.isInfinite(doubles[row])) {
                        throw new SemanticException("FLOAT 列第 " + row + " 行必须为有限数值");
                    }
                } else if (raw instanceof String) {
                    throw new SemanticException(
                            "FLOAT 列第 " + row + " 行收到字符串（拒绝隐式字符串转数值）");
                } else {
                    throw new SemanticException(
                            "FLOAT 列第 " + row + " 行收到非法值: " + raw);
                }
            }
            case TEXT -> {
                if (raw instanceof String s) {
                    strings[row] = s;
                } else {
                    throw new SemanticException(
                            "TEXT 列第 " + row + " 行只接受 JSON 字符串（拒绝隐式数值转字符串），实际: "
                                    + raw.getClass().getSimpleName());
                }
            }
        }
    }

    public DataType type() {
        return type;
    }

    public int rowCount() {
        return nulls.length;
    }

    public boolean isNull(int row) {
        return nulls[row];
    }

    public long getLong(int row) {
        return longs[row];
    }

    public double getDouble(int row) {
        return doubles[row];
    }

    public String getString(int row) {
        return strings[row];
    }

    boolean[] nullBitmap() {
        return nulls;
    }

    long[] longArray() {
        return longs;
    }

    double[] doubleArray() {
        return doubles;
    }

    String[] stringArray() {
        return strings;
    }
}

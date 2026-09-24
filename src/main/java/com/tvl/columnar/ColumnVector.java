package com.tvl.columnar;

import com.tvl.types.DataType;
import com.tvl.types.Values;

import java.util.Arrays;

/**
 * 列式向量：一个类型确定的值数组 + 一张 NULL 位图。
 * 位图位置为 true 表示该行为 NULL（此时值数组对应位置是任意占位值，绝不读取）。
 *
 * 这是"NULL 位图向量执行"的核心载体：谓词逐批计算为 byte[] 三值向量，
 * NOT/AND/OR 只在字节数组上做位级循环，不产生每行对象。
 */
public final class ColumnVector {

    private final DataType type;
    private final Object values; // long[] / double[] / String[] / Boolean[]
    private final boolean[] nulls;

    public ColumnVector(DataType type, Object values, boolean[] nulls) {
        if (values == null || nulls == null) {
            throw new IllegalArgumentException("values 与 nulls 均不能为 null");
        }
        int len = nulls.length;
        boolean shapeOk =
                (type == DataType.INTEGER && values instanceof long[] && ((long[]) values).length == len)
                        || (type == DataType.DOUBLE && values instanceof double[] && ((double[]) values).length == len)
                        || (type == DataType.STRING && values instanceof String[] && ((String[]) values).length == len)
                        || (type == DataType.BOOLEAN && values instanceof Boolean[] && ((Boolean[]) values).length == len);
        if (!shapeOk) {
            throw new IllegalArgumentException("值数组类型/长度与声明类型 " + type + " 不符");
        }
        this.type = type;
        this.values = values;
        this.nulls = nulls;
    }

    public DataType type() {
        return type;
    }

    public int size() {
        return nulls.length;
    }

    public boolean[] nullBitmap() {
        return nulls;
    }

    public boolean isNullAt(int i) {
        return nulls[i];
    }

    public long[] longs() {
        return (long[]) values;
    }

    public double[] doubles() {
        return (double[]) values;
    }

    public String[] strings() {
        return (String[]) values;
    }

    public Boolean[] booleans() {
        return (Boolean[]) values;
    }

    /** 取第 i 行标量；NULL 行为 null。 */
    public Object valueAt(int i) {
        if (nulls[i]) {
            return null;
        }
        switch (type) {
            case INTEGER:
                return longs()[i];
            case DOUBLE:
                return doubles()[i];
            case STRING:
                return strings()[i];
            case BOOLEAN:
                return booleans()[i];
            default:
                throw new IllegalStateException(String.valueOf(type));
        }
    }

    /**
     * 把一个标量常量物化成与批同长的列向量。
     */
    public static ColumnVector constant(DataType type, Object value, int size) {
        boolean[] nulls = new boolean[size];
        switch (type) {
            case INTEGER: {
                long[] arr = new long[size];
                if (value != null) {
                    Arrays.fill(arr, Values.getLong(value));
                } else {
                    Arrays.fill(nulls, true);
                }
                return new ColumnVector(DataType.INTEGER, arr, nulls);
            }
            case DOUBLE: {
                double[] arr = new double[size];
                if (value != null) {
                    Arrays.fill(arr, Values.getDouble(value));
                } else {
                    Arrays.fill(nulls, true);
                }
                return new ColumnVector(DataType.DOUBLE, arr, nulls);
            }
            case STRING: {
                String[] arr = new String[size];
                if (value != null) {
                    Arrays.fill(arr, (String) value);
                } else {
                    Arrays.fill(nulls, true);
                }
                return new ColumnVector(DataType.STRING, arr, nulls);
            }
            case BOOLEAN: {
                Boolean[] arr = new Boolean[size];
                if (value != null) {
                    Arrays.fill(arr, (Boolean) value);
                } else {
                    Arrays.fill(nulls, true);
                }
                return new ColumnVector(DataType.BOOLEAN, arr, nulls);
            }
            default:
                throw new IllegalStateException(String.valueOf(type));
        }
    }
}

package com.tvl.engine;

import com.tvl.types.DataType;

import java.util.function.IntPredicate;

/**
 * 比较运算的唯一定义点：向量执行器与逐行解释器都调用这里，
 * 这样"对照测试"比较的是两种执行方式，而不是两套比较逻辑。
 */
public final class CompareOps {

    private CompareOps() {
    }

    /** 返回比较语义谓词：cmp 是 Comparable.compareTo 风格的结果（负数/0/正数）。 */
    public static IntPredicate predicate(String op) {
        switch (op) {
            case "=":
                return c -> c == 0;
            case "<>":
                return c -> c != 0;
            case "<":
                return c -> c < 0;
            case "<=":
                return c -> c <= 0;
            case ">":
                return c -> c > 0;
            case ">=":
                return c -> c >= 0;
            default:
                throw new IllegalArgumentException("未知比较运算符: " + op);
        }
    }

    public static int compareNonNull(Object a, Object b, DataType common) {
        if (common.isNumeric()) {
            return Double.compare(((Number) a).doubleValue(), ((Number) b).doubleValue());
        }
        if (common == DataType.STRING) {
            return ((String) a).compareTo((String) b);
        }
        return Boolean.compare((Boolean) a, (Boolean) b);
    }

    /** 比较两侧的公共类型；null 表示两侧都是无类型裸 NULL。 */
    public static DataType resolveType(DataType a, DataType b) {
        if (a != null && b != null) {
            DataType common = DataType.commonNumeric(a, b);
            return common != null ? common : a; // 同族时 a==b；裸 NULL 已由分析器拦截跨族
        }
        return a != null ? a : b;
    }
}

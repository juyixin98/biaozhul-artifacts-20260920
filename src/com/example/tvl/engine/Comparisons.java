package com.example.tvl.engine;

import com.example.tvl.engine.QueryPlanner.Slot;

/**
 * 比较运算的公共语义，逐行与向量两条路径共用同一套标量规则，
 * 保证参考实现与向量化实现结果定义一致。
 *
 * 规则：任一操作数为 NULL → UNKNOWN；否则整型之间按 long 精确比较，
 * 其余数值组合提升为 double 比较；文本按字典序。
 */
final class Comparisons {

    private Comparisons() {}

    static SqlBool compare(long lv, long rv, String op) {
        return orderToBool(Long.compare(lv, rv), op);
    }

    static SqlBool compare(double lv, double rv, String op) {
        return orderToBool(Double.compare(lv, rv), op);
    }

    static SqlBool compare(String lv, String rv, String op) {
        return orderToBool(lv.compareTo(rv), op);
    }

    private static SqlBool orderToBool(int order, String op) {
        boolean result = switch (op) {
            case "=" -> order == 0;
            case "<>" -> order != 0;
            case "<" -> order < 0;
            case "<=" -> order <= 0;
            case ">" -> order > 0;
            case ">=" -> order >= 0;
            default -> throw new AssertionError("未知运算符: " + op);
        };
        return result ? SqlBool.TRUE : SqlBool.FALSE;
    }

    /**
     * 读取槽位在指定行上的值；返回 null 表示 SQL NULL。
     * 静态类型由 slot 决定，运行期载体类型由列/常量/参数统一为 Long/Double/String。
     */
    static Object read(Slot slot, Batch batch, int row, ParameterSet params) {
        if (slot instanceof QueryPlanner.ColumnSlot cs) {
            Column c = batch.column(cs.index());
            if (c.isNull(row)) {
                return null;
            }
            return switch (c.type()) {
                case INTEGER -> c.getLong(row);
                case FLOAT -> c.getDouble(row);
                case TEXT -> c.getString(row);
            };
        }
        if (slot instanceof QueryPlanner.ConstSlot ct) {
            if (ct.untypedNull()) {
                return null;
            }
            return ct.value().value(); // 带类型 NULL 时也是 null
        }
        if (slot instanceof QueryPlanner.ParamSlot ps) {
            Value v = params.get(ps.index());
            return v.isNull() ? null : v.value();
        }
        throw new AssertionError("未知槽位: " + slot);
    }
}

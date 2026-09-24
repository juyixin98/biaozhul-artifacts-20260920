package com.tvl.types;

/**
 * 列类型。本执行器是强类型的：只允许同族（数值之间）的隐式提升，
 * 字符串与数值/布尔之间不允许任何隐式转换。
 */
public enum DataType {
    INTEGER("INTEGER"),
    DOUBLE("DOUBLE"),
    STRING("STRING"),
    BOOLEAN("BOOLEAN");

    private final String sqlName;

    DataType(String sqlName) {
        this.sqlName = sqlName;
    }

    public String sqlName() {
        return sqlName;
    }

    public boolean isNumeric() {
        return this == INTEGER || this == DOUBLE;
    }

    public static DataType parse(String text) {
        if (text == null) {
            throw new IllegalArgumentException("类型名不能为空");
        }
        switch (text.trim().toUpperCase()) {
            case "INTEGER":
            case "INT":
            case "BIGINT":
            case "LONG":
                return INTEGER;
            case "DOUBLE":
            case "FLOAT":
            case "REAL":
                return DOUBLE;
            case "STRING":
            case "VARCHAR":
            case "TEXT":
                return STRING;
            case "BOOLEAN":
            case "BOOL":
                return BOOLEAN;
            default:
                throw new IllegalArgumentException("未知类型: " + text);
        }
    }

    /**
     * 两个数值类型的公共类型；任一不是数值则返回 null。
     * INTEGER+INTEGER -> INTEGER，否则 -> DOUBLE。
     */
    public static DataType commonNumeric(DataType a, DataType b) {
        if (a == null || b == null || !a.isNumeric() || !b.isNumeric()) {
            return null;
        }
        return a == DOUBLE || b == DOUBLE ? DOUBLE : INTEGER;
    }
}

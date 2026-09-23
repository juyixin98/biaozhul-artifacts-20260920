package com.example.tvl.engine;

import com.example.tvl.sql.DataType;

/**
 * 带类型的标量值。{@code value == null} 表示该类型的 NULL
 * （无类型 NULL 仅出现在 SQL 字面量中，见 {@link com.example.tvl.sql.Ast.Literal}）。
 */
public record Value(DataType type, Object value) {

    public boolean isNull() {
        return value == null;
    }

    public long asLong() {
        return (Long) value;
    }

    public double asDouble() {
        return ((Number) value).doubleValue();
    }

    public String asText() {
        return (String) value;
    }
}

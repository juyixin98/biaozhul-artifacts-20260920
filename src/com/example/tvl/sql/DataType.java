package com.example.tvl.sql;

/**
 * 列值类型。布尔值不作为存储类型：布尔只以三值逻辑谓词结果的形式存在
 * （TRUE / FALSE / UNKNOWN），见 {@link com.example.tvl.engine.SqlBool}。
 */
public enum DataType {
    INTEGER, // 64 位有符号整数
    FLOAT,   // 双精度浮点数
    TEXT;    // UTF-8 字符串

    public static DataType parse(String raw) {
        if (raw == null) {
            throw new IllegalArgumentException("类型不能为空");
        }
        return switch (raw.trim().toUpperCase()) {
            case "INTEGER", "INT", "BIGINT", "LONG" -> INTEGER;
            case "FLOAT", "DOUBLE", "REAL" -> FLOAT;
            case "TEXT", "STRING", "VARCHAR" -> TEXT;
            default -> throw new IllegalArgumentException("未知类型: " + raw);
        };
    }
}

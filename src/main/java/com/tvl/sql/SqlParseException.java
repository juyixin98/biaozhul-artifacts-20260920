package com.tvl.sql;

/**
 * SQL 解析错误（位置/语法层面）。
 */
public class SqlParseException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public SqlParseException(String message) {
        super(message);
    }
}

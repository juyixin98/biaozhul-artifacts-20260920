package com.example.tvl.sql;

/** SQL 语法错误（编译期）。 */
public class SqlParseException extends RuntimeException {
    public SqlParseException(String message) {
        super(message);
    }
}

package com.example.tvl.engine;

/** 语义错误：未知列、类型不匹配、参数数量/类型错误等。 */
public class SemanticException extends RuntimeException {
    public SemanticException(String message) {
        super(message);
    }
}

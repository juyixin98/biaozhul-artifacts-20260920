package com.tvl.types;

/**
 * 强类型转换错误。
 */
public class TypeCheckException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public TypeCheckException(String message) {
        super(message);
    }
}

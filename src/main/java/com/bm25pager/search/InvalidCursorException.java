package com.bm25pager.search;

/** 游标字符串无法解码（格式损坏/字段缺失/类型错误）。HTTP 层映射为 400。 */
public class InvalidCursorException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    public InvalidCursorException(String message) {
        super(message);
    }
}

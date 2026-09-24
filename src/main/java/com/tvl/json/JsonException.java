package com.tvl.json;

/** JSON 解析错误。 */
public class JsonException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public JsonException(String message) {
        super(message);
    }
}

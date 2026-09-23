package com.example.tvl.json;

/** JSON 解析错误。 */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}

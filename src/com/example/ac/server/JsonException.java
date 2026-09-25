package com.example.ac.server;

/** JSON 解析错误 → HTTP 400。 */
public class JsonException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    public JsonException(String message) {
        super(message);
    }
}

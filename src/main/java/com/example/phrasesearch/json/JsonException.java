package com.example.phrasesearch.json;

/** JSON 解析错误。 */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}

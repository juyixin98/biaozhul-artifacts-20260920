package dev.example.cp.json;

/** JSON 解析 / 序列化错误。 */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}

package engine.json;

/** JSON 解析/构造错误。 */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}

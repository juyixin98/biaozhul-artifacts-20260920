package streamagg.json;

/** JSON 解析/校验错误。 */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}

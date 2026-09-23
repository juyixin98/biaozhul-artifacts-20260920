package cdcrebuild.model;

/** 请求体 / 事件本身不合法（HTTP 400）。 */
public class ValidationException extends RuntimeException {
    public ValidationException(String message) {
        super(message);
    }
}

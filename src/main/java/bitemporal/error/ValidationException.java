package bitemporal.error;

/** 输入数据不合法（缺字段、时间倒置等）。 */
public class ValidationException extends RuntimeException {
    public ValidationException(String message) {
        super(message);
    }
}

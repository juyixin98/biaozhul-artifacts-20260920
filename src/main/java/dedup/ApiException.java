package dedup;

/** A typed API failure; the HTTP layer renders it as {@code {error, message}}. */
public class ApiException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    private final ErrorCode code;

    public ApiException(ErrorCode code, String message) {
        super(message);
        this.code = code;
    }

    public ErrorCode code() {
        return code;
    }

    static ApiException invalidBody(String message) {
        return new ApiException(ErrorCode.INVALID_BODY, message);
    }
}

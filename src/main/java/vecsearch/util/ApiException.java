package vecsearch.util;

/** 带 HTTP 状态码的业务异常，API 层据此返回 4xx。 */
public class ApiException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    private final int status;

    public ApiException(int status, String message) {
        super(message);
        this.status = status;
    }

    public int status() {
        return status;
    }

    public static ApiException badRequest(String message) {
        return new ApiException(400, message);
    }

    public static ApiException notFound(String message) {
        return new ApiException(404, message);
    }

    public static ApiException conflict(String message) {
        return new ApiException(409, message);
    }
}

package streammatch.service;

/** 请求校验/语义错误，携带建议的 HTTP 状态码。 */
public class ApiException extends RuntimeException {

    private final int status;
    private final String code;

    public ApiException(int status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    public int status() {
        return status;
    }

    public String code() {
        return code;
    }

    public static ApiException badRequest(String message) {
        return new ApiException(400, "BAD_REQUEST", message);
    }

    public static ApiException unprocessable(String code, String message) {
        return new ApiException(422, code, message);
    }
}

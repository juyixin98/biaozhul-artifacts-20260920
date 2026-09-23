package com.example.paginate.web;

/**
 * 业务层错误，携带 HTTP 状态码与稳定的错误码。
 */
public class ApiException extends RuntimeException {

    private final int httpStatus;
    private final String code;

    public ApiException(int httpStatus, String code, String message) {
        super(message);
        this.httpStatus = httpStatus;
        this.code = code;
    }

    public int httpStatus() {
        return httpStatus;
    }

    public String code() {
        return code;
    }

    // ---- 常用工厂 ----

    public static ApiException badRequest(String code, String message) {
        return new ApiException(400, code, message);
    }

    public static ApiException forbidden(String code, String message) {
        return new ApiException(403, code, message);
    }

    public static ApiException notFound(String code, String message) {
        return new ApiException(404, code, message);
    }

    public static ApiException gone(String code, String message) {
        return new ApiException(410, code, message);
    }

    public static ApiException conflict(String code, String message) {
        return new ApiException(409, code, message);
    }

    public static ApiException badJson(String message) {
        return new ApiException(400, "invalid_json", message);
    }
}

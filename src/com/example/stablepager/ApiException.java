package com.example.stablepager;

import java.util.LinkedHashMap;
import java.util.Map;

/** Error carrying an HTTP status and a stable machine-readable code. */
public final class ApiException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    private final int status;
    private final String code;
    private final Map<String, Object> details = new LinkedHashMap<>();

    public ApiException(int status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    public ApiException with(String key, Object value) {
        details.put(key, value);
        return this;
    }

    public int status() {
        return status;
    }

    public String code() {
        return code;
    }

    public Map<String, Object> details() {
        return details;
    }

    public static ApiException badRequest(String code, String message) {
        return new ApiException(400, code, message);
    }
}

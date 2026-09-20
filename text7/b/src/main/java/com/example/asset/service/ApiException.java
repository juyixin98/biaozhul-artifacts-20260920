package com.example.asset.service;

import org.springframework.http.HttpStatus;

/** 业务异常基类，携带 HTTP 状态码，由全局异常处理器统一转换为错误响应。 */
public abstract class ApiException extends RuntimeException {

    private final HttpStatus status;

    protected ApiException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public HttpStatus status() {
        return status;
    }

    /** 404 资源不存在 */
    public static class NotFound extends ApiException {
        public NotFound(String message) {
            super(HttpStatus.NOT_FOUND, message);
        }
    }

    /** 409 并发冲突（版本不匹配、期间已关账等） */
    public static class Conflict extends ApiException {
        public Conflict(String message) {
            super(HttpStatus.CONFLICT, message);
        }
    }

    /** 422 非法状态转换 */
    public static class InvalidTransition extends ApiException {
        public InvalidTransition(String message) {
            super(HttpStatus.UNPROCESSABLE_ENTITY, message);
        }
    }

    /** 400 请求参数/业务校验失败 */
    public static class BadRequest extends ApiException {
        public BadRequest(String message) {
            super(HttpStatus.BAD_REQUEST, message);
        }
    }
}

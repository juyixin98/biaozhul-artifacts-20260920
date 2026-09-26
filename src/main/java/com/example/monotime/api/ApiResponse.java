package com.example.monotime.api;

/**
 * 统一 API 响应封装。
 *
 * @param success 是否成功
 * @param data 成功时的载荷（失败为 null）
 * @param error 失败时的错误信息（成功为 null）
 */
public record ApiResponse(boolean success, Object data, String error) {

    public static ApiResponse ok(Object data) {
        return new ApiResponse(true, data, null);
    }

    public static ApiResponse failure(String error) {
        return new ApiResponse(false, null, error);
    }
}

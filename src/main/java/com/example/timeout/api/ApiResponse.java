package com.example.timeout.api;

/** 统一响应信封：{@code success} + {@code data} 或 {@code error}。 */
public record ApiResponse<T>(boolean success, T data, ApiError error) {

    public static <T> ApiResponse<T> ok(T data) {
        return new ApiResponse<>(true, data, null);
    }

    public static <T> ApiResponse<T> error(String code, String message) {
        return new ApiResponse<>(false, null, new ApiError(code, message));
    }

    public record ApiError(String code, String message) {
    }
}

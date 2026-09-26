package com.example.recur.model;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/** 失败响应。errorCode 取值：VALIDATION_ERROR / EXPANSION_LIMIT_EXCEEDED / INVALID_JSON。 */
@JsonPropertyOrder({"success", "tzdbVersion", "errorCode", "message"})
public record ErrorResponse(
    boolean success,
    String tzdbVersion,
    String errorCode,
    String message) {
}

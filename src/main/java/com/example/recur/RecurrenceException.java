package com.example.recur;

/** 业务可预期错误，errorCode 直接映射到 ErrorResponse。 */
public class RecurrenceException extends RuntimeException {
  private final String errorCode;

  public RecurrenceException(String errorCode, String message) {
    super(message);
    this.errorCode = errorCode;
  }

  public String errorCode() {
    return errorCode;
  }
}

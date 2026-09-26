package com.example.timeout.api;

/** 400 类错误：输入在系统边界校验失败。消息可安全返回给调用方。 */
public class BadRequestException extends RuntimeException {
    public BadRequestException(String message) {
        super(message);
    }
}

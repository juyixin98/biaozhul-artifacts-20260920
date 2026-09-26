package com.example.timeout.store;

/** 存储读写失败。 */
public class StoreException extends RuntimeException {
    public StoreException(String message, Throwable cause) {
        super(message, cause);
    }
}

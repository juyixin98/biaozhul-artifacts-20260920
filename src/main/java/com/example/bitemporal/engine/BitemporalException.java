package com.example.bitemporal.engine;

/** 业务规则冲突（如 INSERT 与已有版本重叠）或输入不合法。 */
public class BitemporalException extends RuntimeException {
    public BitemporalException(String message) {
        super(message);
    }
}

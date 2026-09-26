package com.example.hlc.core;

/**
 * Thrown when the logical counter exceeds the configured maximum and the
 * clock is configured with {@link OverflowPolicy#THROW}.
 */
public final class LogicalOverflowException extends RuntimeException {

    public LogicalOverflowException(String message) {
        super(message);
    }
}

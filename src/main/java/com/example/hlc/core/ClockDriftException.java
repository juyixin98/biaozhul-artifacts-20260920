package com.example.hlc.core;

/**
 * Thrown when a received remote timestamp is further ahead of the local wall
 * clock than the configured maximum allowed drift.
 */
public final class ClockDriftException extends RuntimeException {

    public ClockDriftException(String message) {
        super(message);
    }
}

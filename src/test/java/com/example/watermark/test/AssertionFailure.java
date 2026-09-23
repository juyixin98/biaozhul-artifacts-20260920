package com.example.watermark.test;

/**
 * Thrown on a failed assertion in the mini test harness.
 */
public class AssertionFailure extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public AssertionFailure(String message) {
        super(message);
    }
}

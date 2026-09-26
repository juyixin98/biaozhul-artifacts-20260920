package com.example.recurrence.core;

/** Supported frequencies of the explicit recurrence subset. */
public enum Frequency {
    DAILY,
    WEEKLY,
    MONTHLY;

    public static Frequency fromString(String value) {
        for (Frequency f : values()) {
            if (f.name().equals(value)) {
                return f;
            }
        }
        throw new ValidationException(
                "INVALID_FREQ", "freq must be one of DAILY, WEEKLY, MONTHLY, got: " + value);
    }
}

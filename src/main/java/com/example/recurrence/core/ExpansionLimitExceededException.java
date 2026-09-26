package com.example.recurrence.core;

/** Raised when the number of occurrences inside the window exceeds maxExpansions. */
public class ExpansionLimitExceededException extends RuntimeException {

    public ExpansionLimitExceededException(int limit) {
        super("expansion produced more than " + limit
                + " occurrences inside the window; narrow the window or raise maxExpansions");
    }
}

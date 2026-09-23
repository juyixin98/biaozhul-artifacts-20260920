package com.example.drvb.core;

/**
 * Thrown by {@link RuleRegistry} for publication and lookup failures.
 * The {@link RuleErrorCode} is stable and surfaced verbatim in JSON errors.
 */
public class RuleRegistryException extends RuntimeException {

    private final RuleErrorCode code;

    public RuleRegistryException(RuleErrorCode code, String message) {
        super(message);
        this.code = code;
    }

    public RuleErrorCode code() {
        return code;
    }
}

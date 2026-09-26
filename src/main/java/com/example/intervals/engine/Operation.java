package com.example.intervals.engine;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The set-algebra operations accepted in expression nodes, including their
 * JSON aliases. Every operation is either unary or binary.
 */
public enum Operation {
    UNION("union", 2, "or"),
    INTERSECTION("intersection", 2, "and"),
    DIFFERENCE("difference", 2, "minus", "except"),
    COMPLEMENT("complement", 1, "not");

    private final String canonical;
    private final int arity;
    private final String[] aliases;

    Operation(String canonical, int arity, String... aliases) {
        this.canonical = canonical;
        this.arity = arity;
        this.aliases = aliases;
    }

    public String canonical() {
        return canonical;
    }

    public int arity() {
        return arity;
    }

    private static final Map<String, Operation> BY_TOKEN = buildTokenMap();

    private static Map<String, Operation> buildTokenMap() {
        Map<String, Operation> map = new LinkedHashMap<>();
        for (Operation op : values()) {
            map.put(op.canonical, op);
            for (String alias : op.aliases) {
                map.put(alias, op);
            }
        }
        return Map.copyOf(map);
    }

    /** @return the operation for a token, or throws {@code unknown_operation}. */
    public static Operation require(String token) {
        Operation op = BY_TOKEN.get(token);
        if (op == null) {
            throw new IntervalException(ErrorCode.UNKNOWN_OPERATION,
                    "unknown operation '" + token + "'; expected one of " + BY_TOKEN.keySet());
        }
        return op;
    }
}

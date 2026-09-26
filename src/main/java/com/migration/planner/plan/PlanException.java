package com.migration.planner.plan;

import java.util.List;
import java.util.Map;

/** Structured failure of a planning operation, mapped to a JSON error response. */
public class PlanException extends RuntimeException {

    public enum Code {
        UNKNOWN_VERSION,
        NO_PATH,
        INVALID_REQUEST,
        GRAPH_ERROR
    }

    private final Code code;
    private final Map<String, Object> details;

    public PlanException(Code code, String message) {
        this(code, message, Map.of());
    }

    public PlanException(Code code, String message, Map<String, Object> details) {
        super(message);
        this.code = code;
        this.details = details == null ? Map.of() : Map.copyOf(details);
    }

    public Code code() {
        return code;
    }

    public Map<String, Object> details() {
        return details;
    }

    public static PlanException unknownVersion(String version) {
        return new PlanException(Code.UNKNOWN_VERSION,
                "unknown schema version: " + version,
                Map.of("version", version));
    }

    public static PlanException noPath(String from, String to,
                                       List<String> reachable,
                                       List<Map<String, Object>> blockedEdges) {
        return new PlanException(Code.NO_PATH,
                "no migration path from " + from + " to " + to,
                Map.of("from", from, "to", to,
                        "reachableVersions", reachable,
                        "edgesBlockedByPreconditions", blockedEdges));
    }
}

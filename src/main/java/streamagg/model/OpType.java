package streamagg.model;

import java.util.Locale;

/** The three lifecycle operations addressable by event ID. */
public enum OpType {
    /** A new event appears. */
    ADD,
    /** A previously added event is removed. */
    RETRACT,
    /** An existing event's key/value is corrected. */
    CORRECT;

    public static OpType parse(String raw) {
        if (raw == null) {
            throw new IllegalArgumentException("missing op");
        }
        String up = raw.trim().toUpperCase(Locale.ROOT);
        return switch (up) {
            case "ADD", "UPSERT", "INSERT" -> ADD;
            case "RETRACT", "DELETE", "REMOVE", "UNDO" -> RETRACT;
            case "CORRECT", "UPDATE", "AMEND", "FIX" -> CORRECT;
            default -> throw new IllegalArgumentException("unknown op: " + raw);
        };
    }
}

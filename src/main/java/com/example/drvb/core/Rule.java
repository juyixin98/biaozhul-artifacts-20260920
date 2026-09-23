package com.example.drvb.core;

import java.util.Map;
import java.util.Objects;

/**
 * An immutable named filter rule.
 *
 * <p>A rule matches events of one {@code eventType} whose payload satisfies
 * {@code condition}. {@code action} is a free-form label copied onto matches
 * (e.g. {@code "BLOCK"}, {@code "REVIEW"}); the engine never interprets it.
 */
public final class Rule {

    private final String id;
    private final String name;
    private final String eventType;
    private final Condition condition;
    private final String action;
    private final boolean enabled;

    public Rule(String id, String name, String eventType, Condition condition,
                String action, boolean enabled) {
        this.id = requireNonBlank(id, "rule id");
        this.name = requireNonBlank(name, "rule name");
        this.eventType = requireNonBlank(eventType, "eventType");
        this.condition = Objects.requireNonNull(condition, "condition");
        this.action = action == null ? "" : action;
        this.enabled = enabled;
    }

    public String id() {
        return id;
    }

    public String name() {
        return name;
    }

    public String eventType() {
        return eventType;
    }

    public Condition condition() {
        return condition;
    }

    public String action() {
        return action;
    }

    public boolean enabled() {
        return enabled;
    }

    public boolean matches(Event event) {
        return enabled && eventType.equals(event.type()) && condition.test(event);
    }

    /** Canonical map, keyed in a fixed order, used for checksums. */
    Map<String, Object> toMap() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("id", id);
        m.put("name", name);
        m.put("eventType", eventType);
        m.put("condition", condition.toMap());
        m.put("action", action);
        m.put("enabled", enabled);
        return m;
    }

    public static Rule fromMap(Map<String, Object> m) {
        Object cond = m.get("condition");
        if (!(cond instanceof Map<?, ?> cm)) {
            throw new IllegalArgumentException("rule requires a 'condition' object");
        }
        boolean enabled = !Boolean.FALSE.equals(m.get("enabled"));
        return new Rule(
                (String) m.get("id"),
                (String) m.getOrDefault("name", m.get("id")),
                (String) m.get("eventType"),
                Condition.fromMap(cast(cm)),
                (String) m.get("action"),
                enabled);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> cast(Map<?, ?> m) {
        return (Map<String, Object>) m;
    }

    private static String requireNonBlank(String s, String what) {
        if (s == null || s.isBlank()) {
            throw new IllegalArgumentException(what + " is required");
        }
        return s;
    }
}

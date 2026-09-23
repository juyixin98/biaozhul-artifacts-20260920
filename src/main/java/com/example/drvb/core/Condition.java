package com.example.drvb.core;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * A rule condition: a small, JSON-friendly predicate tree evaluated against an
 * {@link Event}.
 *
 * <p>Supported opcodes:
 * <ul>
 *   <li>logic: {@code all} (AND), {@code any} (OR), {@code not} (single child);</li>
 *   <li>comparison: {@code eq}, {@code ne}, {@code gt}, {@code gte}, {@code lt},
 *       {@code lte} — numeric comparisons promote both sides to {@link BigDecimal},
 *       other values are compared with {@code equals};</li>
 *   <li>strings: {@code contains}, {@code startsWith}, {@code endsWith},
 *       {@code matches} (full-match regular expression);</li>
 *   <li>membership / presence: {@code in}, {@code exists},
 *       {@code notExists};</li>
 *   <li>{@code alwaysTrue} — leaf predicate matching every event.</li>
 * </ul>
 *
 * <p>Conditions are immutable and validated at construction time.
 */
public sealed interface Condition permits Condition.Logic, Condition.Compare,
        Condition.StringOp, Condition.In, Condition.Exists, Condition.True {

    boolean test(Event event);

    // ------------------------------------------------------------------ nodes

    /** Logical combinator. {@code not} uses exactly one child. */
    record Logic(String op, List<Condition> children) implements Condition {
        public Logic {
            children = List.copyOf(children);
            if ("not".equals(op)) {
                if (children.size() != 1) {
                    throw new IllegalArgumentException("'not' requires exactly one child");
                }
            } else if (!"all".equals(op) && !"any".equals(op)) {
                throw new IllegalArgumentException("unknown logic op: " + op);
            }
        }

        @Override
        public boolean test(Event event) {
            return switch (op) {
                case "all" -> children.stream().allMatch(c -> c.test(event));
                case "any" -> children.stream().anyMatch(c -> c.test(event));
                case "not" -> !children.get(0).test(event);
                default -> throw new IllegalStateException(op);
            };
        }

        @Override
        public Map<String, Object> toMap() {
            Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("op", op);
            if ("not".equals(op)) {
                m.put("condition", children.get(0).toMap());
            } else {
                List<Object> kids = new ArrayList<>(children.size());
                for (Condition c : children) {
                    kids.add(c.toMap());
                }
                m.put("conditions", kids);
            }
            return m;
        }
    }

    /** Comparison leaf. Numeric operands are compared with BigDecimal precision. */
    record Compare(String op, String field, Object value) implements Condition {
        public Compare {
            requireOpcode(op, "eq", "ne", "gt", "gte", "lt", "lte");
            if (field == null || field.isBlank()) {
                throw new IllegalArgumentException("compare requires a field path");
            }
            if (value == null) {
                throw new IllegalArgumentException("compare requires a non-null value");
            }
        }

        @Override
        public boolean test(Event event) {
            Object actual = Paths.resolve(event.payload(), field);
            if (actual == null) {
                return false;
            }
            return switch (op) {
                case "eq" -> valueEquals(actual, value);
                case "ne" -> !valueEquals(actual, value);
                case "gt", "gte", "lt", "lte" -> {
                    BigDecimal a = asDecimal(actual);
                    BigDecimal b = asDecimal(value);
                    if (a == null || b == null) {
                        yield false;
                    }
                    int cmp = a.compareTo(b);
                    yield switch (op) {
                        case "gt" -> cmp > 0;
                        case "gte" -> cmp >= 0;
                        case "lt" -> cmp < 0;
                        default -> cmp <= 0;
                    };
                }
                default -> throw new IllegalStateException(op);
            };
        }

        @Override
        public Map<String, Object> toMap() {
            Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("op", op);
            m.put("field", field);
            m.put("value", value);
            return m;
        }
    }

    /** String leaf: both operands must be strings. */
    record StringOp(String op, String field, String value) implements Condition {
        public StringOp {
            requireOpcode(op, "contains", "startsWith", "endsWith", "matches");
            if (field == null || field.isBlank()) {
                throw new IllegalArgumentException("string op requires a field path");
            }
            if (value == null) {
                throw new IllegalArgumentException("string op requires a value");
            }
        }

        @Override
        public boolean test(Event event) {
            if (Paths.resolve(event.payload(), field) instanceof String s) {
                return switch (op) {
                    case "contains" -> s.contains(value);
                    case "startsWith" -> s.startsWith(value);
                    case "endsWith" -> s.endsWith(value);
                    case "matches" -> Pattern.matches(value, s);
                    default -> throw new IllegalStateException(op);
                };
            }
            return false;
        }

        @Override
        public Map<String, Object> toMap() {
            Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("op", op);
            m.put("field", field);
            m.put("value", value);
            return m;
        }
    }

    /** Membership leaf: field value must equal one of {@code values}. */    record In(String field, List<Object> values) implements Condition {
        public In {
            if (field == null || field.isBlank()) {
                throw new IllegalArgumentException("in requires a field path");
            }
            values = List.copyOf(values);
        }

        @Override
        public boolean test(Event event) {
            Object actual = Paths.resolve(event.payload(), field);
            if (actual == null) {
                return false;
            }
            for (Object v : values) {
                if (valueEquals(actual, v)) {
                    return true;
                }
            }
            return false;
        }

        @Override
        public Map<String, Object> toMap() {
            Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("op", "in");
            m.put("field", field);
            m.put("values", values);
            return m;
        }
    }

    /** Presence leaf. A present JSON {@code null} is treated as absent. */
    record Exists(String field, boolean negate) implements Condition {
        public Exists {
            if (field == null || field.isBlank()) {
                throw new IllegalArgumentException("exists requires a field path");
            }
        }

        @Override
        public boolean test(Event event) {
            boolean present = Paths.resolve(event.payload(), field) != null;
            return negate != present;
        }

        @Override
        public Map<String, Object> toMap() {
            Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("op", negate ? "notExists" : "exists");
            m.put("field", field);
            return m;
        }
    }

    /** Leaf matching every event. */
    record True() implements Condition {
        @Override
        public boolean test(Event event) {
            return true;
        }

        @Override
        public Map<String, Object> toMap() {
            return Map.of("op", "alwaysTrue");
        }
    }

    // ------------------------------------------------------------ toMap

    /** Canonical, JSON-ready representation used for checksums and serialization. */
    java.util.Map<String, Object> toMap();

    private static void requireOpcode(String op, String... allowed) {
        for (String a : allowed) {
            if (a.equals(op)) {
                return;
            }
        }
        throw new IllegalArgumentException("unsupported op: " + op);
    }

    private static boolean valueEquals(Object actual, Object expected) {
        if (expected == null) {
            return false;
        }
        BigDecimal a = asDecimal(actual);
        BigDecimal b = asDecimal(expected);
        if (a != null && b != null) {
            return a.compareTo(b) == 0;
        }
        return actual.equals(expected);
    }

    private static BigDecimal asDecimal(Object o) {
        if (o instanceof Number n) {
            return new BigDecimal(n.toString());
        }
        if (o instanceof String s) {
            try {
                return new BigDecimal(s);
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
    }

    /**
     * Builds a {@link Condition} from a loose, Jackson-style map (lists and
     * maps, strings, numbers, booleans). Kept intentionally explicit so rule
     * authoring errors surface with precise messages.
     */
    public static Condition fromMap(Map<String, Object> m) {
        String op = (String) m.get("op");
        if (op == null) {
            throw new IllegalArgumentException("condition requires 'op'");
        }
        return switch (op) {
            case "all", "any" -> {
                List<Condition> kids = childList(m.get("conditions"));
                yield new Logic(op, kids);
            }
            case "not" -> {
                Object child = m.get("condition");
                if (!(child instanceof Map<?, ?> cm)) {
                    throw new IllegalArgumentException("'not' requires a 'condition' object");
                }
                yield new Logic("not", List.of(fromMap(castMap(cm))));
            }
            case "eq", "ne", "gt", "gte", "lt", "lte" ->
                    new Compare(op, (String) m.get("field"), m.get("value"));
            case "contains", "startsWith", "endsWith", "matches" -> {
                Object value = m.get("value");
                if (!(value instanceof String s)) {
                    throw new IllegalArgumentException(op + " requires a string 'value'");
                }
                yield new StringOp(op, (String) m.get("field"), s);
            }
            case "in" -> {
                Object values = m.get("values");
                if (!(values instanceof Collection<?> col)) {
                    throw new IllegalArgumentException("'in' requires a 'values' list");
                }
                yield new In((String) m.get("field"), new ArrayList<>(col));
            }
            case "exists" -> new Exists((String) m.get("field"), false);
            case "notExists" -> new Exists((String) m.get("field"), true);
            case "alwaysTrue" -> new True();
            default -> throw new IllegalArgumentException("unknown condition op: " + op);
        };
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> castMap(Map<?, ?> m) {
        return (Map<String, Object>) m;
    }

    private static List<Condition> childList(Object raw) {
        if (!(raw instanceof Collection<?> col)) {
            throw new IllegalArgumentException("expected a 'conditions' list");
        }
        List<Condition> out = new ArrayList<>(col.size());
        for (Object o : col) {
            if (!(o instanceof Map<?, ?> cm)) {
                throw new IllegalArgumentException("each condition must be an object");
            }
            out.add(fromMap(castMap(cm)));
        }
        return out;
    }
}

package hllengine.engine;

import hllengine.api.ApiException;
import hllengine.json.Json;

import java.util.List;
import java.util.Map;

/**
 * A predicate over a row. Built from the JSON request fragment
 * {@code {"op": "...", "field": "...", "value": ...}}.
 *
 * <p>Supported operators: {@code eq}, {@code ne}, {@code lt}, {@code le},
 * {@code gt}, {@code ge}, {@code isnull}, {@code notnull}, {@code in},
 * {@code nin}, {@code like} (exact substring containment, no wildcards),
 * plus the combinators {@code and} / {@code or} (each carrying an {@code "args"}
 * list) and {@code not} (carrying {@code "arg"}).
 */
public interface Predicate {

    boolean test(Map<String, Object> row);

    @SuppressWarnings("unchecked")
    static Predicate fromJson(Object node) {
        Map<String, Object> obj = Json.asObject(node, "predicate");
        String op = Json.asString(obj.get("op"), "predicate.op");
        switch (op) {
            case "and":
            case "or": {
                List<Object> args = Json.asArray(obj.get("args"), "predicate.args");
                Predicate[] parts = new Predicate[args.size()];
                for (int k = 0; k < parts.length; k++) {
                    parts[k] = fromJson(args.get(k));
                }
                boolean isAnd = op.equals("and");
                return row -> {
                    for (Predicate p : parts) {
                        boolean r = p.test(row);
                        if (isAnd != r) return !isAnd;
                    }
                    return isAnd;
                };
            }
            case "not": {
                Object argNode = obj.get("arg") != null ? obj.get("arg") : obj.get("predicate");
                Predicate inner = fromJson(argNode);
                return row -> !inner.test(row);
            }
            default:
                break;
        }
        String field = Json.asString(obj.get("field"), "predicate.field for op \"" + op + "\"");
        Object wanted = obj.get("value");
        switch (op) {
            case "isnull":
                return row -> row.get(field) == null;
            case "notnull":
                return row -> row.get(field) != null;
            case "like":
            case "in":
            case "nin":
            case "eq":
            case "ne":
            case "lt":
            case "le":
            case "gt":
            case "ge":
                if (!obj.containsKey("value")) {
                    throw new ApiException(ApiException.BAD_FORMAT,
                            "predicate op \"" + op + "\" requires a \"value\" field");
                }
                break;
            default:
                throw new ApiException(ApiException.BAD_REQUEST,
                        "unknown predicate operator \"" + op + "\"");
        }
        switch (op) {
            case "eq":
                return row -> Values.equal(row.get(field), wanted);
            case "ne":
                return row -> !Values.equal(row.get(field), wanted);
            case "lt":
                return row -> Values.compare(row.get(field), wanted) < 0;
            case "le":
                return row -> Values.compare(row.get(field), wanted) <= 0;
            case "gt":
                return row -> Values.compare(row.get(field), wanted) > 0;
            case "ge":
                return row -> Values.compare(row.get(field), wanted) >= 0;
            case "like": {
                String needle = Json.asString(wanted, "predicate.value for like");
                return row -> {
                    Object actual = row.get(field);
                    return actual instanceof String && ((String) actual).contains(needle);
                };
            }
            case "in":
            case "nin": {
                List<Object> set = Json.asArray(wanted, "predicate.value for " + op);
                boolean in = op.equals("in");
                return row -> {
                    for (Object option : set) {
                        if (Values.equal(row.get(field), option)) return in;
                    }
                    return !in;
                };
            }
            default:
                throw new ApiException(ApiException.BAD_REQUEST,
                        "unknown predicate operator \"" + op + "\"");
        }
    }
}

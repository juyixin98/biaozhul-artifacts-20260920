package hllengine.engine;

import hllengine.api.ApiException;
import hllengine.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * One requested aggregate over a group, e.g.
 * {@code {"fn":"hll_distinct","field":"uid","precision":12,"alias":"uv"}}.
 *
 * <p>Supported functions:
 * <ul>
 *   <li>{@code count} (field may be "*" or omitted: counts rows),</li>
 *   <li>{@code count_distinct} &mdash; <b>exact</b> set-based count (small data),</li>
 *   <li>{@code hll_distinct} &mdash; <b>approximate</b> cardinality via HLL,
 *       optionally with {@code precision} (4..18) and {@code seed};</li>
 *   <li>{@code sum}, {@code avg}, {@code min}, {@code max}.</li>
 * </ul>
 *
 * The alias defaults to a generated name. The {@code hll_distinct} result is
 * always accompanied by a sibling {@code <alias>_error} value carrying the
 * nominal relative standard error, so approximate results can never be
 * mistaken for exact ones.
 */
public final class AggSpec {

    public enum Kind {
        COUNT, COUNT_DISTINCT_EXACT, HLL_DISTINCT, SUM, AVG, MIN, MAX
    }

    private final String fn;
    private final Kind kind;
    private final String field;
    private final int precision;
    private final int seed;
    private final String alias;

    private AggSpec(String fn, Kind kind, String field, int precision, int seed, String alias) {
        this.fn = fn;
        this.kind = kind;
        this.field = field;
        this.precision = precision;
        this.seed = seed;
        this.alias = alias;
    }

    public String fn() {
        return fn;
    }

    public Kind kind() {
        return kind;
    }

    public String field() {
        return field;
    }

    public int precision() {
        return precision;
    }

    public int seed() {
        return seed;
    }

    public String alias() {
        return alias;
    }

    public String errorAlias() {
        return alias + "_relativeStandardError";
    }

    /** Whether this aggregate's value is an approximation rather than an exact result. */
    public boolean approximate() {
        return kind == Kind.HLL_DISTINCT;
    }

    @SuppressWarnings("unchecked")
    public static AggSpec parse(Object node, int index) {
        String path = "aggregates[" + index + "]";
        Map<String, Object> obj = Json.asObject(node, path);
        String fn = Json.asString(obj.get("fn"), path + ".fn");
        String field = obj.get("field") == null ? "*" : Json.asString(obj.get("field"), path + ".field");

        Kind kind;
        switch (fn) {
            case "count": kind = Kind.COUNT; break;
            case "count_distinct": kind = Kind.COUNT_DISTINCT_EXACT; break;
            case "hll_distinct": kind = Kind.HLL_DISTINCT; break;
            case "sum": kind = Kind.SUM; break;
            case "avg": kind = Kind.AVG; break;
            case "min": kind = Kind.MIN; break;
            case "max": kind = Kind.MAX; break;
            default:
                throw new ApiException(ApiException.BAD_REQUEST,
                        "unknown aggregate function \"" + fn + "\" in " + path);
        }
        int precision = hllengine.hll.HllConfig.DEFAULT_PRECISION;
        int seed = hllengine.hll.HllConfig.DEFAULT_SEED;
        if (kind == Kind.HLL_DISTINCT) {
            if (obj.containsKey("precision")) {
                precision = Json.asInt(obj.get("precision"), path + ".precision",
                        hllengine.hll.HllConfig.MIN_PRECISION, hllengine.hll.HllConfig.MAX_PRECISION);
            }
            if (obj.containsKey("seed")) {
                seed = (int) Json.asLong(obj.get("seed"), path + ".seed");
            }
            if ("*".equals(field)) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        path + ".field is required for hll_distinct");
            }
        } else {
            if (obj.containsKey("precision") || obj.containsKey("seed")) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        "precision/seed may only be specified for hll_distinct in " + path);
            }
        }
        String alias = obj.get("alias") == null
                ? defaultAlias(fn, field, index)
                : Json.asString(obj.get("alias"), path + ".alias");
        if (alias.endsWith("_relativeStandardError")) {
            throw new ApiException(ApiException.BAD_REQUEST,
                    "alias \"" + alias + "\" is reserved for the error column");
        }
        return new AggSpec(fn, kind, field, precision, seed, alias);
    }

    private static String defaultAlias(String fn, String field, int index) {
        return ("*".equals(field) ? fn : fn + "_" + field) + (index == 0 ? "" : "_" + index);
    }

    public Map<String, Object> describe() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("fn", fn);
        m.put("field", field);
        if (kind == Kind.HLL_DISTINCT) {
            m.put("precision", precision);
            m.put("seed", seed);
            m.put("approximate", true);
            m.put("nominalRelativeStandardError",
                    hllengine.hll.HllConfig.of(precision, seed).relativeStandardError());
        } else {
            m.put("approximate", false);
        }
        m.put("alias", alias);
        m.put("errorAlias", approximate() ? errorAlias() : null);
        return m;
    }

    /** Validates that aliases are unique within the aggregate list. */
    public static void validateUnique(List<AggSpec> specs) {
        List<String> seen = new ArrayList<>();
        for (AggSpec s : specs) {
            if (seen.contains(s.alias())) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        "duplicate aggregate alias \"" + s.alias() + "\"");
            }
            seen.add(s.alias());
        }
    }
}

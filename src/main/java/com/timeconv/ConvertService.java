package com.timeconv;

import com.timeconv.json.JsonNumber;

import java.math.BigInteger;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Request/response service shared by the HTTP API and the test suite.
 * Accepts and returns plain Java maps (JSON-shaped), so it can be tested in-process.
 */
public final class ConvertService {

    /**
     * Handle {@code POST /convert}.
     * Request:  {"value": "1500", "fromUnit": "MILLISECOND", "toUnit": "SECOND", "rounding": "HALF_UP"}
     * Response: {"ok": true, "result": "2", "unit": "SECOND", "exact": false}
     */
    public Map<String, Object> convert(Map<String, Object> req) {
        String valueText = requiredString(req, "value");
        Unit from = Unit.parse(requiredString(req, "fromUnit"));
        Unit to = Unit.parse(requiredString(req, "toUnit"));
        Rounding mode = Rounding.parse(optionalString(req, "rounding"));

        BigInteger value = parseInteger(valueText);
        TimeConverter.Result r = TimeConverter.convert(value, from, to, mode);
        long checked = TimeConverter.toLongChecked(r.value());

        Map<String, Object> out = ok();
        out.put("result", Long.toString(checked));
        out.put("unit", to.name());
        out.put("exact", r.exact());
        return out;
    }

    /**
     * Handle {@code POST /parse}.
     * Request:  {"text": "1969-12-31T23:59:59.5Z", "format": "ISO_INSTANT", "toUnit": "MILLISECOND"}
     *       or: {"text": "-0.5", "format": "DECIMAL", "unit": "SECOND", "toUnit": "NANOSECOND"}
     * Response: {"ok": true, "result": "-500", "unit": "MILLISECOND", "exact": true}
     */
    public Map<String, Object> parse(Map<String, Object> req) {
        String text = requiredString(req, "text");
        String format = optionalString(req, "format");
        if (format == null) {
            format = text.contains("T") ? "ISO_INSTANT" : "DECIMAL";
        }
        Unit to = Unit.parse(requiredString(req, "toUnit"));
        Rounding mode = Rounding.parse(optionalString(req, "rounding"));

        TimeConverter.Result r = switch (format.trim().toUpperCase()) {
            case "DECIMAL" -> {
                Unit unit = Unit.parse(requiredString(req, "unit"));
                yield TextParser.parseDecimal(text, unit, to, mode);
            }
            case "ISO_INSTANT", "ISO", "ISO8601" -> TextParser.parseIsoInstant(text, to, mode);
            default -> throw new ConvertException(ErrorCode.BAD_REQUEST,
                    "unsupported format: '" + format + "' (supported: DECIMAL, ISO_INSTANT)");
        };
        long checked = TimeConverter.toLongChecked(r.value());

        Map<String, Object> out = ok();
        out.put("result", Long.toString(checked));
        out.put("unit", to.name());
        out.put("exact", r.exact());
        return out;
    }

    /** Handle {@code GET /meta}: capabilities and the recorded tzdb version. */
    public Map<String, Object> meta() {
        Map<String, Object> out = ok();
        out.put("service", "time-precision-converter");
        out.put("tzdbVersion", TzdbInfo.version());
        out.put("units", java.util.List.of("SECOND", "MILLISECOND", "MICROSECOND", "NANOSECOND"));
        out.put("roundingModes", java.util.List.of("UP", "DOWN", "CEILING", "FLOOR", "HALF_UP", "HALF_EVEN", "UNNECESSARY"));
        out.put("integerRange", "int64 (results outside this range return OVERFLOW)");
        return out;
    }

    /** Build the standard error envelope. */
    public static Map<String, Object> error(ErrorCode code, String message) {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("code", code.name());
        err.put("message", message);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ok", false);
        out.put("error", err);
        return out;
    }

    private static Map<String, Object> ok() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ok", true);
        return out;
    }

    private static String requiredString(Map<String, Object> req, String key) {
        String v = optionalString(req, key);
        if (v == null) {
            throw new ConvertException(ErrorCode.BAD_REQUEST, "missing required field: '" + key + "'");
        }
        return v;
    }

    private static String optionalString(Map<String, Object> req, String key) {
        Object v = req.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof String s) {
            return s;
        }
        if (v instanceof JsonNumber n) {
            return n.raw();
        }
        throw new ConvertException(ErrorCode.BAD_REQUEST,
                "field '" + key + "' must be a string or number, got: " + v.getClass().getSimpleName());
    }

    private static BigInteger parseInteger(String text) {
        try {
            return new BigInteger(text.trim());
        } catch (NumberFormatException e) {
            throw new ConvertException(ErrorCode.INVALID_VALUE,
                    "value must be an integer (use /parse for fractional text): '" + text + "'");
        }
    }
}

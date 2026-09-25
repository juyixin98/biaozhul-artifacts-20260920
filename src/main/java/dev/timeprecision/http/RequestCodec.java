package dev.timeprecision.http;

import dev.timeprecision.ConversionException;
import dev.timeprecision.ErrorCode;
import dev.timeprecision.TimeConverter;
import dev.timeprecision.TimeUnit;
import dev.timeprecision.json.JsonValue;

import java.math.BigInteger;
import java.math.RoundingMode;
import java.util.Locale;

/** Translates decoded JSON request bodies into typed call parameters. */
final class RequestCodec {

    record ConvertParams(BigInteger value, TimeUnit from, TimeUnit to, RoundingMode rounding) {
    }

    record ParseParams(String text, TimeUnit to, RoundingMode rounding) {
    }

    record FormatParams(BigInteger value, TimeUnit unit) {
    }

    private RequestCodec() {
    }

    static ConvertParams decodeConvert(JsonValue body) {
        JsonValue.Obj obj = requireObject(body);
        BigInteger value = integerField(obj, "value");
        TimeUnit from = TimeUnit.fromName(stringField(obj, "fromUnit"));
        TimeUnit to = TimeUnit.fromName(stringField(obj, "toUnit"));
        return new ConvertParams(value, from, to, roundingField(obj));
    }

    static ParseParams decodeParse(JsonValue body) {
        JsonValue.Obj obj = requireObject(body);
        String text = stringField(obj, "text");
        TimeUnit to = TimeUnit.fromName(stringField(obj, "toUnit"));
        return new ParseParams(text, to, roundingField(obj));
    }

    static FormatParams decodeFormat(JsonValue body) {
        JsonValue.Obj obj = requireObject(body);
        BigInteger value = integerField(obj, "value");
        TimeUnit unit = TimeUnit.fromName(stringField(obj, "unit"));
        return new FormatParams(value, unit);
    }

    private static JsonValue.Obj requireObject(JsonValue body) {
        if (!(body instanceof JsonValue.Obj obj)) {
            throw new ConversionException(ErrorCode.BAD_REQUEST, "request body must be a JSON object");
        }
        return obj;
    }

    private static String stringField(JsonValue.Obj obj, String key) {
        JsonValue v = obj.get(key);
        if (v instanceof JsonValue.Str s) {
            return s.value();
        }
        throw new ConversionException(ErrorCode.BAD_REQUEST,
                "field '" + key + "' is required and must be a string");
    }

    /**
     * Accepts an integer as a JSON string ("-1500") or as an integer JSON
     * number literal (-1500). Fractional/exponent literals are rejected so
     * values never pass through floating point.
     */
    private static BigInteger integerField(JsonValue.Obj obj, String key) {
        JsonValue v = obj.get(key);
        if (v instanceof JsonValue.Str s) {
            return TimeConverter.parseBigInteger(s.value());
        }
        if (v instanceof JsonValue.Num n) {
            String raw = n.raw();
            if (raw.contains(".") || raw.contains("e") || raw.contains("E")) {
                throw new ConversionException(ErrorCode.INVALID_VALUE,
                        "field '" + key + "' must be an integer; fractional or "
                                + "exponent literals are not accepted: " + raw);
            }
            return TimeConverter.parseBigInteger(raw);
        }
        throw new ConversionException(ErrorCode.BAD_REQUEST,
                "field '" + key + "' is required and must be an integer string or number");
    }

    /** Rounding is optional; the default UNNECESSARY fails loudly on lossy conversions. */
    private static RoundingMode roundingField(JsonValue.Obj obj) {
        JsonValue v = obj.get("rounding");
        if (v == null || v == JsonValue.Null.INSTANCE) {
            return RoundingMode.UNNECESSARY;
        }
        if (!(v instanceof JsonValue.Str s)) {
            throw new ConversionException(ErrorCode.BAD_REQUEST, "field 'rounding' must be a string");
        }
        try {
            return RoundingMode.valueOf(s.value().trim().toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw new ConversionException(ErrorCode.INVALID_VALUE,
                    "unknown rounding mode: " + s.value()
                            + " (expected one of FLOOR, CEILING, DOWN, UP, HALF_UP, HALF_DOWN, HALF_EVEN, UNNECESSARY)");
        }
    }
}

package hllengine.hll;

import java.nio.charset.StandardCharsets;

/**
 * Deterministic, type-tagged byte encoding of JSON values for hashing.
 *
 * <p>Each value is prefixed with a one-byte type tag so that values of
 * different JSON types can never collide (e.g. the number {@code 1}, the
 * string {@code "1"} and the boolean {@code true} hash differently). Strings
 * are UTF-8 encoded.
 */
public final class ValueCoding {

    public static final byte TAG_NULL = 0x01;
    public static final byte TAG_FALSE = 0x02;
    public static final byte TAG_TRUE = 0x03;
    public static final byte TAG_LONG = 0x04;
    public static final byte TAG_DOUBLE = 0x05;
    public static final byte TAG_STRING = 0x06;

    private ValueCoding() {
    }

    /**
     * Encodes a value produced by {@link hllengine.json.Json} parsing
     * (null, Boolean, Long, Integer, Double, String) or any {@link Number}.
     */
    public static byte[] encode(Object value) {
        if (value == null) {
            return new byte[] {TAG_NULL};
        }
        if (value instanceof Boolean) {
            return new byte[] {(Boolean) value ? TAG_TRUE : TAG_FALSE};
        }
        if (value instanceof Long || value instanceof Integer) {
            byte[] out = new byte[9];
            out[0] = TAG_LONG;
            long n = ((Number) value).longValue();
            for (int k = 0; k < 8; k++) {
                out[8 - k] = (byte) (n >>> (8 * k));
            }
            return out;
        }
        if (value instanceof Number) {
            byte[] out = new byte[9];
            out[0] = TAG_DOUBLE;
            long bits = Double.doubleToLongBits(((Number) value).doubleValue());
            for (int k = 0; k < 8; k++) {
                out[8 - k] = (byte) (bits >>> (8 * k));
            }
            return out;
        }
        if (value instanceof String) {
            byte[] utf8 = ((String) value).getBytes(StandardCharsets.UTF_8);
            byte[] out = new byte[utf8.length + 1];
            out[0] = TAG_STRING;
            System.arraycopy(utf8, 0, out, 1, utf8.length);
            return out;
        }
        throw new IllegalArgumentException("cannot hash value of type " + value.getClass().getSimpleName());
    }
}

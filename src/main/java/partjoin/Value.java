package partjoin;

import java.math.BigDecimal;
import java.math.BigInteger;

/**
 * A scalar column value.
 *
 * Equality follows SQL-style rules used by the join engine:
 *  - NULL never equals anything (including another NULL);
 *  - numbers of different wire types (integer / floating point / decimal)
 *    compare by numeric value, so 1, 1.0 and 1.00 are equal;
 *  - strings, booleans and numbers are never equal across type families
 *    (the string "1" does not equal the number 1).
 */
public final class Value {

    public static final Value NULL = new Value(null);

    private final Object raw; // null, Boolean, Long, Double, BigDecimal, String

    private Value(Object raw) {
        this.raw = raw;
    }

    public static Value of(Object o) {
        if (o == null) return NULL;
        if (o instanceof Value v) return v;
        if (o instanceof Boolean || o instanceof String || o instanceof Long
                || o instanceof Double || o instanceof BigDecimal) {
            return new Value(o);
        }
        if (o instanceof Integer || o instanceof Short || o instanceof Byte) {
            return new Value(((Number) o).longValue());
        }
        if (o instanceof Float f) {
            return new Value(f.doubleValue());
        }
        if (o instanceof BigInteger bi) {
            return new Value(new BigDecimal(bi));
        }
        throw new JoinException(JoinException.INVALID_REQUEST,
                "Unsupported value type: " + o.getClass().getName());
    }

    public Object raw() {
        return raw;
    }

    public boolean isNull() {
        return raw == null;
    }

    public boolean isNumber() {
        return raw instanceof Number;
    }

    public boolean equalsValue(Value other) {
        if (raw == null || other.raw == null) return false; // NULL <> NULL
        if (raw instanceof Number && other.raw instanceof Number) {
            return toBigDecimal(raw).compareTo(toBigDecimal(other.raw)) == 0;
        }
        return raw.equals(other.raw);
    }

    public int hashValue() {
        if (raw == null) return 0;
        if (raw instanceof Number n) return canonical(n).hashCode();
        return raw.hashCode();
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof Value other && equalsValue(other);
    }

    @Override
    public int hashCode() {
        return hashValue();
    }

    @Override
    public String toString() {
        return raw == null ? "null" : raw.toString();
    }

    static BigDecimal toBigDecimal(Object n) {
        if (n instanceof BigDecimal bd) return bd;
        if (n instanceof Double || n instanceof Float) {
            return BigDecimal.valueOf(((Number) n).doubleValue());
        }
        if (n instanceof BigInteger bi) return new BigDecimal(bi);
        return BigDecimal.valueOf(((Number) n).longValue());
    }

    /** Scale-independent canonical form so 1, 1.0, 1.00 hash together. */
    private static BigDecimal canonical(Number n) {
        BigDecimal bd = toBigDecimal(n).stripTrailingZeros();
        if (bd.scale() < 0) bd = bd.setScale(0);
        return bd;
    }
}

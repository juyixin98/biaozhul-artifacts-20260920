package colscan;

/** A cell value: either a non-null 64-bit integer or NULL. NULL is never zero. */
public final class Value {
    public static final Value NULL = new Value(0L, false);

    public final long longValue;
    public final boolean present;

    private Value(long longValue, boolean present) {
        this.longValue = longValue;
        this.present = present;
    }

    public static Value of(long v) {
        return new Value(v, true);
    }

    @Override
    public String toString() {
        return present ? Long.toString(longValue) : "NULL";
    }
}

package com.timeconv;

/**
 * Explicit rounding modes for lossy unit conversions.
 * The mode is mandatory whenever a conversion is not exact; there is no silent default.
 */
public enum Rounding {
    /** Round away from zero. */
    UP,
    /** Round toward zero (truncation). */
    DOWN,
    /** Round toward positive infinity. */
    CEILING,
    /** Round toward negative infinity. */
    FLOOR,
    /** Round to nearest; ties go away from zero. */
    HALF_UP,
    /** Round to nearest; ties go to the even neighbour. */
    HALF_EVEN,
    /** Require the conversion to be exact; fail otherwise. This is the implicit default. */
    UNNECESSARY;

    /**
     * @throws ConvertException with code INVALID_ROUNDING if the name is not supported
     */
    public static Rounding parse(String name) {
        if (name == null || name.isBlank()) {
            // No rounding specified means: the caller asserts the conversion is exact.
            return UNNECESSARY;
        }
        try {
            return Rounding.valueOf(name.trim().toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new ConvertException(ErrorCode.INVALID_ROUNDING,
                    "unsupported rounding mode: '" + name + "' (supported: UP, DOWN, CEILING, FLOOR, HALF_UP, HALF_EVEN, UNNECESSARY)");
        }
    }
}

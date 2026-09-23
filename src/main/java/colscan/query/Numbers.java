package colscan.query;

/** 数值比较：两侧都是整数按 long 精确比较，否则升级为 double。 */
final class Numbers {

    private Numbers() {}

    static int compare(Number a, Number b) {
        if (isIntegral(a) && isIntegral(b)) {
            return Long.compare(a.longValue(), b.longValue());
        }
        return Double.compare(a.doubleValue(), b.doubleValue());
    }

    private static boolean isIntegral(Number n) {
        return n instanceof Long || n instanceof Integer || n instanceof Short
                || n instanceof Byte;
    }
}

package intervalindex;

/** 极简断言工具（无第三方依赖）。 */
final class Check {

    private Check() {}

    static void that(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    static void eq(long actual, long expected, String message) {
        if (actual != expected) {
            throw new AssertionError(message + " — expected=" + expected + ", actual=" + actual);
        }
    }

    static void eq(Object actual, Object expected, String message) {
        if (actual == null ? expected != null : !actual.equals(expected)) {
            throw new AssertionError(message + " — expected=" + expected + ", actual=" + actual);
        }
    }

    static void fail(String message) {
        throw new AssertionError(message);
    }
}

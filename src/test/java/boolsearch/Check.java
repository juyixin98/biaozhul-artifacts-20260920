package boolsearch;

import java.util.Objects;

/** 断言助手。 */
public final class Check {
    private Check() {}

    public static void isTrue(boolean b, String msg) {
        if (!b) throw new AssertionError(msg);
    }

    public static void eq(Object actual, Object expected, String msg) {
        if (!Objects.equals(actual, expected)) {
            throw new AssertionError(msg + " | 期望=" + expected + " 实际=" + actual);
        }
    }
}

package com.tjoin;

/**
 * 迷你测试断言工具（零依赖，不引入 JUnit）。
 */
public final class Asserts {

    private Asserts() {
    }

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertFalse(boolean cond, String msg) {
        assertTrue(!cond, msg);
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(msg + " — expected:<" + expected + "> but was:<" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + " — expected:<" + expected + "> but was:<" + actual + ">");
        }
    }

    public static void assertNull(Object o, String msg) {
        if (o != null) {
            throw new AssertionError(msg + " — expected null but was:<" + o + ">");
        }
    }

    public static void assertNotNull(Object o, String msg) {
        if (o == null) {
            throw new AssertionError(msg);
        }
    }

    public static void fail(String msg) {
        throw new AssertionError(msg);
    }

    /** 断言可运行块抛出指定类型异常，返回该异常以便继续断言。 */
    public static <T extends Throwable> T assertThrows(Class<T> type, Runnable r, String msg) {
        try {
            r.run();
        } catch (Throwable t) {
            if (type.isInstance(t)) {
                return type.cast(t);
            }
            throw new AssertionError(msg + " — expected exception " + type.getName()
                    + " but got " + t, t);
        }
        throw new AssertionError(msg + " — expected exception " + type.getName() + " but nothing was thrown");
    }
}

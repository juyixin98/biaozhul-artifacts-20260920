package sessions.testing;

import java.util.List;
import java.util.Map;
import java.util.Objects;

/** 极简断言集合；对象比较对 Map/List/Number 做递归语义比较（忽略 Integer/Long 装箱差异）。 */
public final class Assert {

    private Assert() {
    }

    public static void assertTrue(boolean condition, String message) {
        if (!condition) {
            throw new AssertionFailure(message);
        }
    }

    public static void assertFalse(boolean condition, String message) {
        assertTrue(!condition, message);
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionFailure(message + " — expected <" + expected
                    + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (!semanticEquals(expected, actual)) {
            throw new AssertionFailure(message + " — expected <" + expected
                    + "> but was <" + actual + ">");
        }
    }

    public static void fail(String message) {
        throw new AssertionFailure(message);
    }

    /** 递归语义相等：数字按数值（整数按 long、浮点按 double），Map/List 逐元素递归。 */
    private static boolean semanticEquals(Object a, Object b) {
        if (a instanceof Number an && b instanceof Number bn) {
            boolean floating = a instanceof Double || a instanceof Float
                    || b instanceof Double || b instanceof Float;
            return floating
                    ? Double.compare(an.doubleValue(), bn.doubleValue()) == 0
                    : an.longValue() == bn.longValue();
        }
        if (a instanceof Map<?, ?> am && b instanceof Map<?, ?> bm) {
            if (am.size() != bm.size()) {
                return false;
            }
            for (Map.Entry<?, ?> e : am.entrySet()) {
                if (!bm.containsKey(e.getKey())
                        || !semanticEquals(e.getValue(), bm.get(e.getKey()))) {
                    return false;
                }
            }
            return true;
        }
        if (a instanceof List<?> al && b instanceof List<?> bl) {
            if (al.size() != bl.size()) {
                return false;
            }
            for (int i = 0; i < al.size(); i++) {
                if (!semanticEquals(al.get(i), bl.get(i))) {
                    return false;
                }
            }
            return true;
        }
        return Objects.equals(a, b);
    }
}

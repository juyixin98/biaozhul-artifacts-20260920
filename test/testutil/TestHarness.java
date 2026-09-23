package testutil;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖微型测试框架：记录断言失败，最后统一输出。
 * 不使用 JUnit，保证仅靠 JDK 即可运行全部验收测试。
 */
public final class TestHarness {

    private final String suiteName;
    private final List<String> failures = new ArrayList<>();
    private int checks = 0;

    public TestHarness(String suiteName) {
        this.suiteName = suiteName;
    }

    public void check(boolean condition, String message) {
        checks++;
        if (!condition) {
            failures.add(message);
        }
    }

    public void eq(Object expected, Object actual, String message) {
        boolean equal;
        if (expected != null && actual != null
                && expected.getClass().isArray() && actual.getClass().isArray()) {
            equal = java.util.Arrays.deepEquals(
                    toObjectArray(expected), toObjectArray(actual));
        } else {
            equal = java.util.Objects.equals(expected, actual);
        }
        check(equal, message + " — 期望 <" + display(expected)
                + ">，实际 <" + display(actual) + ">");
    }

    private static Object[] toObjectArray(Object array) {
        if (array instanceof Object[] o) {
            return o;
        }
        int len = java.lang.reflect.Array.getLength(array);
        Object[] out = new Object[len];
        for (int i = 0; i < len; i++) {
            out[i] = java.lang.reflect.Array.get(array, i);
        }
        return out;
    }

    private static String display(Object v) {
        if (v != null && v.getClass().isArray()) {
            return java.util.Arrays.deepToString(toObjectArray(v));
        }
        return String.valueOf(v);
    }

    public void fail(String message) {
        check(false, message);
    }

    /** 输出结果；全部通过返回 true。 */
    public boolean report() {
        if (failures.isEmpty()) {
            System.out.printf("[PASS] %s（%d 项断言）%n", suiteName, checks);
            return true;
        }
        System.out.printf("[FAIL] %s：%d/%d 项断言失败%n", suiteName, failures.size(), checks);
        for (String f : failures) {
            System.out.println("   - " + f);
        }
        return false;
    }

    public int failureCount() {
        return failures.size();
    }
}

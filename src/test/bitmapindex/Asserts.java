package bitmapindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Tiny zero-dependency assertion harness. */
public final class Asserts {
    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();
    private String currentCase = "";

    public void caseName(String name) {
        currentCase = name;
    }

    public void check(boolean cond, String message) {
        if (cond) {
            passed++;
        } else {
            failed++;
            failures.add("[" + currentCase + "] " + message);
            System.out.println("  FAIL [" + currentCase + "] " + message);
        }
    }

    public void eq(Object actual, Object expected, String message) {
        check(deepEquals(actual, expected),
                message + " — expected=" + expected + " actual=" + actual);
    }

    /** Equality that treats JSON Long/Integer/Double numbers by value. */
    @SuppressWarnings("unchecked")
    public static boolean deepEquals(Object a, Object b) {
        if (a == b) {
            return true;
        }
        if (a == null || b == null) {
            return false;
        }
        if (a instanceof Number && b instanceof Number) {
            Number na = (Number) a;
            Number nb = (Number) b;
            boolean aFloat = na instanceof Double || na instanceof Float;
            boolean bFloat = nb instanceof Double || nb instanceof Float;
            if (aFloat || bFloat) {
                return na.doubleValue() == nb.doubleValue();
            }
            return na.longValue() == nb.longValue();
        }
        if (a instanceof List && b instanceof List) {
            List<Object> la = (List<Object>) a;
            List<Object> lb = (List<Object>) b;
            if (la.size() != lb.size()) {
                return false;
            }
            for (int i = 0; i < la.size(); i++) {
                if (!deepEquals(la.get(i), lb.get(i))) {
                    return false;
                }
            }
            return true;
        }
        if (a instanceof Map && b instanceof Map) {
            Map<Object, Object> ma = (Map<Object, Object>) a;
            Map<Object, Object> mb = (Map<Object, Object>) b;
            if (!ma.keySet().equals(mb.keySet())) {
                return false;
            }
            for (Object k : ma.keySet()) {
                if (!deepEquals(ma.get(k), mb.get(k))) {
                    return false;
                }
            }
            return true;
        }
        return a.equals(b);
    }

    public void eqInt(long actual, long expected, String message) {
        check(actual == expected, message + " — expected=" + expected + " actual=" + actual);
    }

    public int passed() {
        return passed;
    }

    public int failed() {
        return failed;
    }

    public List<String> failures() {
        return failures;
    }

    public void summary(String suite) {
        System.out.printf("%-28s passed=%d failed=%d%n", suite, passed, failed);
    }
}

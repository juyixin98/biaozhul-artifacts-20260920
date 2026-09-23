package sessionwindow;

import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency test harness: assertions + ordered @Test-style cases.
 * Deliberately small so the project stays free of third-party jars.
 */
final class TestRunner {

    static final class AssertionFailure extends RuntimeException {
        private static final long serialVersionUID = 1L;

        AssertionFailure(String message) {
            super(message);
        }
    }

    static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionFailure(message);
        }
    }

    static void fail(String message) {
        throw new AssertionFailure(message);
    }

    static void eq(Object actual, Object expected, String message) {
        boolean equal = actual == null ? expected == null : actual.equals(expected);
        if (!equal) {
            throw new AssertionFailure(message + " — expected <" + expected
                    + "> but was <" + actual + ">");
        }
    }

    static void isTrue(boolean condition, String message) {
        check(condition, message);
    }

    static void isNull(Object actual, String message) {
        check(actual == null, message + " — expected null but was <" + actual + ">");
    }

    static void notNull(Object actual, String message) {
        check(actual != null, message + " — expected non-null");
    }

    @FunctionalInterface
    interface TestBody {
        void run() throws Exception;
    }

    private final List<Case> cases = new ArrayList<>();
    private final String suiteName;

    TestRunner(String suiteName) {
        this.suiteName = suiteName;
    }

    TestRunner test(String name, TestBody body) {
        cases.add(new Case(name, body));
        return this;
    }

    int run() {
        System.out.println("== " + suiteName + " ==");
        int failures = 0;
        for (Case c : cases) {
            try {
                c.body.run();
                System.out.println("  PASS  " + c.name);
            } catch (AssertionFailure e) {
                failures++;
                System.out.println("  FAIL  " + c.name + "\n        " + e.getMessage());
            } catch (Exception e) {
                failures++;
                System.out.println("  ERROR " + c.name + " — " + e);
                e.printStackTrace(System.out);
            }
        }
        System.out.println("-- " + suiteName + ": "
                + (cases.size() - failures) + "/" + cases.size() + " passed --");
        return failures;
    }

    private record Case(String name, TestBody body) {
    }

    static int runAll(List<TestRunner> runners) {
        int total = 0;
        for (TestRunner r : runners) {
            total += r.run();
        }
        System.out.println(total == 0
                ? "\nALL SUITES GREEN"
                : "\n" + total + " FAILURE(S) TOTAL");
        return total == 0 ? 0 : 1;
    }
}

package joinplanner;

import java.util.ArrayList;
import java.util.List;

/**
 * Minimal zero-dependency test harness. Assertions collect failures instead of
 * aborting, and {@link #main} prints a summary and exits non-zero if anything failed.
 * Registered test classes implement static {@code test(Test t)} methods invoked from
 * their own {@code main} (see each test class); the shared assertion API lives here.
 */
public class TestFramework {

    public final List<String> failures = new ArrayList<>();
    private int checks = 0;

    public void check(boolean condition, String message) {
        checks++;
        if (!condition) {
            failures.add(message);
        }
    }

    public void eq(Object actual, Object expected, String message) {
        checks++;
        boolean ok = actual == null ? expected == null : actual.equals(expected);
        if (!ok) {
            failures.add(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public void approx(double actual, double expected, double relTol, String message) {
        checks++;
        double scale = Math.max(1.0, Math.abs(expected));
        if (Math.abs(actual - expected) > relTol * scale) {
            failures.add(message + " — expected ~" + expected + " but was " + actual);
        }
    }

    public int checks() {
        return checks;
    }

    public interface Body {
        void run() throws Exception;
    }

    /** Expects the body to throw an exception whose message contains {@code fragment}. */
    public void throwsContaining(String fragment, Body body) {
        checks++;
        try {
            body.run();
        } catch (Exception e) {
            if (e.getMessage() == null || !e.getMessage().contains(fragment)) {
                failures.add("Expected exception containing '" + fragment
                        + "' but got: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
            return;
        }
        failures.add("Expected exception containing '" + fragment + "' but nothing was thrown");
    }
}

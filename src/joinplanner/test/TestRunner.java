package joinplanner.test;

import java.util.ArrayList;
import java.util.List;

/** Minimal no-dependency test harness: assertions + counters, main() exit code. */
public final class TestRunner {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();
    private String currentSuite = "";

    public void suite(String name) {
        currentSuite = name;
        System.out.println("== " + name);
    }

    public void check(String name, boolean cond) {
        if (cond) {
            passed++;
            System.out.println("   PASS " + name);
        } else {
            failed++;
            failures.add(currentSuite + " :: " + name);
            System.out.println("   FAIL " + name);
        }
    }

    public void checkEq(String name, Object actual, Object expected) {
        boolean ok = expected == null ? actual == null : expected.equals(actual);
        if (!ok) {
            System.out.println("        expected=" + expected + " actual=" + actual);
        }
        check(name, ok);
    }

    public void checkClose(String name, double actual, double expected, double relTol) {
        boolean ok = Double.isNaN(expected)
                ? Double.isNaN(actual)
                : Math.abs(actual - expected) <= relTol * Math.max(1.0, Math.abs(expected));
        if (!ok) {
            System.out.println("        expected~=" + expected + " actual=" + actual);
        }
        check(name, ok);
    }

    public int finish() {
        System.out.println();
        System.out.println("TOTAL " + (passed + failed) + ", PASS " + passed + ", FAIL " + failed);
        if (failed > 0) {
            System.out.println("Failures:");
            failures.forEach(f -> System.out.println("  - " + f));
        }
        return failed == 0 ? 0 : 1;
    }
}

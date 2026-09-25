package neardup;

import java.util.ArrayList;
import java.util.List;

/** Minimal test framework (no JUnit dependency). */
public final class TestRunner {

    public static final class Failure {
        final String group;
        final String name;
        final String message;

        Failure(String group, String name, String message) {
            this.group = group;
            this.name = name;
            this.message = message;
        }
    }

    private static final List<Failure> failures = new ArrayList<>();
    private static int checks = 0;
    private static String currentGroup = "";

    public static void group(String g) {
        currentGroup = g;
    }

    public static void check(boolean cond, String name) {
        checks++;
        if (!cond) {
            failures.add(new Failure(currentGroup, name, "assertion failed"));
            System.out.println("FAIL [" + currentGroup + "] " + name);
        }
    }

    public static void check(boolean cond, String name, String detail) {
        checks++;
        if (!cond) {
            failures.add(new Failure(currentGroup, name, detail));
            System.out.println("FAIL [" + currentGroup + "] " + name + " — " + detail);
        }
    }

    public static void approx(double actual, double expected, double tol, String name) {
        checks++;
        if (Math.abs(actual - expected) > tol) {
            String msg = "expected ~" + expected + " +/- " + tol + " but got " + actual;
            failures.add(new Failure(currentGroup, name, msg));
            System.out.println("FAIL [" + currentGroup + "] " + name + " — " + msg);
        }
    }

    public static int checks() {
        return checks;
    }

    public static List<Failure> failures() {
        return failures;
    }

    public static int finish() {
        System.out.println();
        System.out.println("checks: " + checks + ", failures: " + failures.size());
        if (!failures.isEmpty()) {
            for (Failure f : failures) {
                System.out.println("  - [" + f.group + "] " + f.name + ": " + f.message);
            }
            return 1;
        }
        System.out.println("ALL TESTS PASSED");
        return 0;
    }
}

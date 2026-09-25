package streamagg.test;

import java.util.ArrayList;
import java.util.List;

/**
 * Minimal test runner: register named checks, {@link #run(String[])} executes all
 * (or only those passed as args), prints a TAP-ish report, and exits non-zero on
 * failure. Zero external dependencies.
 */
public final class TestRunner {

    /** A test body that may throw any exception (checked or unchecked). */
    @FunctionalInterface
    public interface TestBody {
        void run() throws Throwable;
    }

    private record TestCase(String name, TestBody body) {
    }

    private final List<TestCase> cases = new ArrayList<>();

    public void test(String name, TestBody body) {
        cases.add(new TestCase(name, body));
    }

    public int run(String[] filter) {
        List<String> only = filter == null ? List.of() : List.of(filter);
        int passed = 0;
        int failed = 0;
        List<String> failures = new ArrayList<>();

        System.out.println("1.." + cases.size());
        int n = 0;
        for (TestCase tc : cases) {
            n++;
            if (!only.isEmpty() && only.stream().noneMatch(tc.name()::contains)) {
                System.out.println("ok " + n + " - " + tc.name() + " # SKIP");
                continue;
            }
            try {
                tc.body().run();
                System.out.println("ok " + n + " - " + tc.name());
                passed++;
            } catch (TestFailure f) {
                System.out.println("not ok " + n + " - " + tc.name());
                System.out.println("    " + f.getMessage().replace("\n", "\n    "));
                failures.add(tc.name() + ": " + f.getMessage());
                failed++;
            } catch (Throwable t) {
                System.out.println("not ok " + n + " - " + tc.name() + " (threw "
                        + t.getClass().getSimpleName() + ")");
                System.out.println("    " + t);
                for (StackTraceElement e : t.getStackTrace()) {
                    if (e.getClassName().startsWith("streamagg")) {
                        System.out.println("      at " + e);
                    }
                }
                failures.add(tc.name() + ": threw " + t);
                failed++;
            }
        }
        System.out.println();
        System.out.println("# tests " + (passed + failed));
        System.out.println("# pass " + passed);
        System.out.println("# fail " + failed);
        if (failed > 0) {
            System.out.println("# FAILURES:");
            for (String f : failures) {
                System.out.println("#   - " + f);
            }
            return 1;
        }
        return 0;
    }
}

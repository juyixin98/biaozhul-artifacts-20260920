package hllengine.test;

import java.io.PrintStream;
import java.util.ArrayList;
import java.util.List;

/**
 * Minimal dependency-free test harness. Each test class registers itself with
 * {@link #register}; a failing assertion records the file/line of the calling
 * test method (via stack walking) without aborting the run.
 */
public final class TestRunner {

    @FunctionalInterface
    public interface TestCase {
        void run(Assert a) throws Exception;
    }

    public interface Suite {
        void register(Registry r);
    }

    public static final class Registry {
        private final List<NamedTest> tests = new ArrayList<>();

        public void add(String name, TestCase tc) {
            tests.add(new NamedTest(name, tc));
        }

        public List<NamedTest> tests() {
            return tests;
        }
    }

    static final class NamedTest {
        final String name;
        final TestCase body;

        NamedTest(String name, TestCase body) {
            this.name = name;
            this.body = body;
        }
    }

    public static final class Assert {
        private final List<String> failures = new ArrayList<>();

        public void check(boolean cond, String message) {
            if (!cond) failures.add(message + " @ " + callerLocation());
        }

        public void fail(String message) {
            check(false, message);
        }

        public void eq(Object actual, Object expected, String what) {
            boolean same = actual == null ? expected == null
                    : (expected == null ? false : actual.equals(expected));
            if (!same) {
                failures.add(what + ": expected <" + expected + "> but was <" + actual + "> @ "
                        + callerLocation());
            }
        }

        public void approx(double actual, double expected, double relTolerance, String what) {
            double tol = Math.abs(expected) * relTolerance;
            if (Math.abs(actual - expected) > tol) {
                failures.add(what + ": expected ~" + expected + " (+-" + (relTolerance * 100)
                        + "%) but was " + actual + " @ " + callerLocation());
            }
        }

        public void withinAbsolute(double actual, double expected, double absTolerance, String what) {
            if (Math.abs(actual - expected) > absTolerance) {
                failures.add(what + ": expected ~" + expected + " (+-" + absTolerance
                        + ") but was " + actual + " @ " + callerLocation());
            }
        }

        private List<String> drain() {
            List<String> copy = new ArrayList<>(failures);
            failures.clear();
            return copy;
        }
    }

    private static String callerLocation() {
        // Walk past every frame inside the harness/assertion infrastructure so
        // we land on the first frame of the actual test method (lambdas keep
        // the enclosing test class name, e.g. HllSketchTest$$Lambda...).
        for (StackTraceElement el : Thread.currentThread().getStackTrace()) {
            String cn = el.getClassName();
            String simple = cn;
            int dollar = cn.indexOf('$');
            if (dollar >= 0) simple = cn.substring(cn.lastIndexOf('.') + 1, dollar);
            if (cn.startsWith("hllengine.test.")
                    && !cn.equals(TestRunner.class.getName())
                    && !cn.equals(Assert.class.getName())
                    && !simple.equals("TestRunner")
                    && !simple.equals("Assert")) {
                return simple + ":" + el.getLineNumber();
            }
        }
        return "(unknown)";
    }

    private static final List<Suite> SUITES = List.of(
            new JsonTest(),
            new HashTest(),
            new HllSketchTest(),
            new SerializationTest(),
            new MergeCompatibilityTest(),
            new QueryEngineTest(),
            new ApiTest()
    );

    /**
     * Runs every registered suite.
     *
     * @return true if all tests passed
     */
    public static boolean runAll(PrintStream out) {
        Registry registry = new Registry();
        for (Suite suite : SUITES) {
            suite.register(registry);
        }
        Assert shared = new Assert();
        int passed = 0, failed = 0;
        List<String> allFailures = new ArrayList<>();
        for (NamedTest t : registry.tests()) {
            try {
                t.body.run(shared);
            } catch (Throwable th) {
                shared.fail("threw " + th.getClass().getSimpleName() + ": " + th.getMessage());
            }
            List<String> failures = shared.drain();
            if (failures.isEmpty()) {
                passed++;
                out.println("PASS " + t.name);
            } else {
                failed++;
                out.println("FAIL " + t.name);
                for (String f : failures) {
                    out.println("       - " + f);
                    allFailures.add(t.name + " :: " + f);
                }
            }
        }
        out.println();
        out.println("----------------------------------------------------");
        out.println("tests: " + (passed + failed) + ", passed: " + passed + ", failed: " + failed);
        if (!allFailures.isEmpty()) {
            out.println();
            out.println("FAILURES:");
            for (String f : allFailures) {
                out.println("  * " + f);
            }
        }
        return failed == 0;
    }
}

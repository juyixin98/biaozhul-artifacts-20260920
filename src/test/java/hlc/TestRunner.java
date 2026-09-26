package hlc;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * Tiny dependency-free test runner. Test classes have a public no-argument constructor
 * (used for per-test setup) and methods annotated with {@link Test}. The JVM exits non-zero
 * if any test fails, so the script can gate on it.
 */
public final class TestRunner {

    private TestRunner() {
    }

    public static void main(String[] args) throws Exception {
        List<Class<?>> classes = new ArrayList<>();
        for (String name : args) {
            classes.add(Class.forName(name));
        }
        int passed = 0;
        int failed = 0;
        List<String> failures = new ArrayList<>();

        for (Class<?> cls : classes) {
            for (Method m : cls.getDeclaredMethods()) {
                if (!m.isAnnotationPresent(Test.class)) {
                    continue;
                }
                String label = cls.getSimpleName() + "." + m.getName();
                try {
                    Object instance = cls.getDeclaredConstructor().newInstance();
                    m.setAccessible(true);
                    m.invoke(instance);
                    System.out.println("  PASS " + label);
                    passed++;
                } catch (java.lang.reflect.InvocationTargetException e) {
                    Throwable cause = e.getCause();
                    System.out.println("  FAIL " + label + " -> " + cause);
                    cause.printStackTrace(System.out);
                    failures.add(label);
                    failed++;
                }
            }
            runAfterAll(cls);
        }
        System.out.println();
        System.out.println("Tests run: " + (passed + failed) + ", passed: " + passed
                + ", failed: " + failed);
        if (failed > 0) {
            System.out.println("FAILED: " + failures);
            System.exit(1);
        }
    }

    private static void runAfterAll(Class<?> cls) {
        for (Method m : cls.getDeclaredMethods()) {
            if (m.isAnnotationPresent(AfterAll.class)) {
                try {
                    m.setAccessible(true);
                    m.invoke(null);
                } catch (Exception e) {
                    System.out.println("  WARN @AfterAll of " + cls.getSimpleName() + " failed: "
                            + e.getCause());
                }
            }
        }
    }

    // ------------------------------------------------------------ assertions

    public static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertTrue(boolean cond) {
        assertTrue(cond, "expected true");
    }

    public static void assertFalse(boolean cond, String message) {
        if (cond) {
            throw new AssertionError(message);
        }
    }

    public static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionError(message + " — expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void assertLess(HLCTimestamp a, HLCTimestamp b, String message) {
        if (!(a.isBefore(b))) {
            throw new AssertionError(message + " — expected " + a + " < " + b);
        }
    }

    public static void fail(String message) {
        throw new AssertionError(message);
    }

    public static <T extends Throwable> T assertThrows(Class<T> type, RunnableThrowing action) {
        try {
            action.run();
        } catch (Throwable t) {
            if (type.isInstance(t)) {
                return type.cast(t);
            }
            throw new AssertionError("expected " + type.getSimpleName() + " but got " + t, t);
        }
        throw new AssertionError("expected " + type.getSimpleName() + " but nothing was thrown");
    }

    @FunctionalInterface
    public interface RunnableThrowing {
        void run() throws Throwable;
    }
}

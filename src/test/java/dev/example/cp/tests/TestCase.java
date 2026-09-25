package dev.example.cp.tests;

import java.util.ArrayList;
import java.util.List;

/** 零依赖的迷你 JUnit：每个测试是 TestCase 的一个子类实例。 */
public abstract class TestCase {

    private final String name;
    private long tmpCounter = 0;

    protected TestCase(String name) {
        this.name = name;
    }

    public String name() {
        return name;
    }

    protected abstract void run() throws Exception;

    // ---------------- 临时目录 ----------------

    protected java.nio.file.Path newDataDir() {
        try {
            java.nio.file.Path base = java.nio.file.Paths.get("build", "test-data");
            java.nio.file.Files.createDirectories(base);
            java.nio.file.Path dir = base.resolve(getClass().getSimpleName()
                    + "-" + (tmpCounter++) + "-" + System.nanoTime());
            java.nio.file.Files.createDirectories(dir);
            return dir;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    protected static void deleteRecursively(java.nio.file.Path root) {
        if (!java.nio.file.Files.exists(root)) {
            return;
        }
        try (var walk = java.nio.file.Files.walk(root)) {
            walk.sorted(java.util.Comparator.reverseOrder()).forEach(p -> {
                try {
                    java.nio.file.Files.deleteIfExists(p);
                } catch (Exception ignored) {
                    // 尽力清理
                }
            });
        } catch (Exception ignored) {
            // 尽力清理
        }
    }

    // ---------------- 断言 ----------------

    protected static void assertTrue(boolean cond, String message) {
        if (!cond) {
            throw new AssertionFailure(message);
        }
    }

    protected static void assertFalse(boolean cond, String message) {
        if (cond) {
            throw new AssertionFailure(message);
        }
    }

    protected static void assertEquals(Object expected, Object actual, String message) {
        if (!java.util.Objects.equals(expected, actual)) {
            throw new AssertionFailure(message + " — expected=<" + expected + "> actual=<" + actual + ">");
        }
    }

    protected static void assertEquals(long expected, long actual, String message) {
        if (expected != actual) {
            throw new AssertionFailure(message + " — expected=<" + expected + "> actual=<" + actual + ">");
        }
    }

    protected static void fail(String message) {
        throw new AssertionFailure(message);
    }

    protected static <T extends Throwable> T assertThrows(Class<T> type, Runnable body, String message) {
        try {
            body.run();
        } catch (RuntimeException | Error e) {
            if (type.isInstance(e)) {
                return type.cast(e);
            }
            throw new AssertionFailure(message + " — wrong exception type: " + e.getClass() + " " + e.getMessage());
        }
        throw new AssertionFailure(message + " — expected " + type + " but nothing was thrown");
    }

    // ---------------- runner ----------------

    public static void main(String[] args) {
        List<TestCase> tests = new ArrayList<>();
        register(tests);
        int passed = 0;
        int failed = 0;
        List<String> failures = new ArrayList<>();
        for (TestCase t : tests) {
            try {
                t.run();
                System.out.println("PASS  " + t.name());
                passed++;
            } catch (Throwable th) {
                System.out.println("FAIL  " + t.name() + "  -> " + th);
                failures.add(t.name() + " :: " + th);
                failed++;
            }
        }
        System.out.println();
        System.out.println("tests: " + tests.size() + ", passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            System.out.println();
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            System.exit(1);
        }
    }

    private static void register(List<TestCase> out) {
        List<Class<?>> testClasses = List.of(
                JsonTest.class,
                StoragePrimitivesTest.class,
                SourceLogTest.class,
                OperatorTest.class,
                EngineHappyPathTest.class,
                EngineFaultMatrixTest.class,
                RepeatedFaultsTest.class,
                UnsafeSideEffectTest.class,
                SchedulersTest.class,
                HttpServiceTest.class);
        for (Class<?> c : testClasses) {
            try {
                var ctor = c.getDeclaredConstructor();
                ctor.setAccessible(true);
                Object instance = ctor.newInstance();
                if (instance instanceof TestCase tc) {
                    out.add(tc);
                }
            } catch (Exception e) {
                throw new RuntimeException("cannot instantiate " + c, e);
            }
        }
    }
}

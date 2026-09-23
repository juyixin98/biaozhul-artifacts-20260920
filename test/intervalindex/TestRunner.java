package intervalindex;

/**
 * 测试入口（零依赖）：依次运行全部测试。
 * 用法：java -cp build/classes:build/test-classes intervalindex.TestRunner
 */
public class TestRunner {

    public static void main(String[] args) {
        long started = System.nanoTime();
        int passed = 0;
        Class<?>[] suites = {
                AvlTreeTest.class,
                StoreTest.class,
                PropertyTest.class,
                HttpIntegrationTest.class,
        };
        for (Class<?> suite : suites) {
            long t0 = System.nanoTime();
            try {
                suite.getMethod("run").invoke(null);
                passed++;
                System.out.printf("  ✓ %s (%.1f ms)%n", suite.getSimpleName(),
                        (System.nanoTime() - t0) / 1e6);
            } catch (Exception e) {
                System.out.printf("  ✗ %s FAILED%n", suite.getSimpleName());
                Throwable cause = e.getCause() != null ? e.getCause() : e;
                cause.printStackTrace(System.out);
                System.out.println("\n" + passed + " suite(s) passed before failure; aborting.");
                System.exit(1);
            }
        }
        System.out.printf("%nAll %d test suites passed in %.1f ms%n",
                passed, (System.nanoTime() - started) / 1e6);
    }
}

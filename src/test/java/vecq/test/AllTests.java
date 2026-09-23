package vecq.test;

/**
 * 测试总入口（无 JUnit 依赖）：
 *   javac -d target/test-classes -cp target/classes src/test/java/vecq/test/*.java
 *   java -cp target/classes:target/test-classes vecq.test.AllTests
 *
 * 退出码：0 全部通过；1 存在失败。
 */
public final class AllTests {

    public static void main(String[] args) {
        long t0 = System.currentTimeMillis();
        run("NullBitmap", NullBitmapTest::run);
        run("SelectionVector", SelectionVectorTest::run);
        run("ColumnTable", ColumnTableTest::run);
        run("JsonRoundTrip", JsonRoundTripTest::run);
        run("Tri", TriTest::run);
        run("Filter", FilterTest::run);
        run("BatchBoundary", BatchBoundaryTest::run);
        run("AllNullColumn", AllNullColumnTest::run);
        run("DuplicateSelection", DuplicateSelectionTest::run);
        run("ProjectAggregate", ProjectAggTest::run);
        run("InvalidSelection", InvalidSelectionTest::run);
        run("RandomDifferential", RandomDifferentialTest::run);
        run("Server", ServerTest::run);

        long elapsed = System.currentTimeMillis() - t0;
        System.out.println();
        System.out.println("================================================");
        System.out.println("通过断言: " + Assert.passed
                + "，失败: " + Assert.failed
                + "，耗时: " + elapsed + " ms");
        if (Assert.failed > 0) {
            System.out.println("存在失败用例。");
            System.exit(1);
        }
        System.out.println("全部通过。");
    }

    private static void run(String name, Runnable r) {
        int before = Assert.failed;
        System.out.println("[RUN ] " + name);
        try {
            r.run();
        } catch (Throwable t) {
            Assert.failed++;
            System.out.println("  [ERROR] " + name + " 抛出未预期异常: "
                    + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        int delta = Assert.failed - before;
        if (delta == 0) {
            System.out.println("[ OK ] " + name);
        } else {
            System.out.println("[FAIL] " + name + "（" + delta + " 个断言失败）");
        }
    }
}

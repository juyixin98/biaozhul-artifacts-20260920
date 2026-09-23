package dev.dedup.hll;

/** 零依赖测试入口：运行全部 TestCase，汇总断言数/失败数，失败时退出码 1。 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        TestCase[] cases = {
                new HashAlgorithmTest(),
                new HllSketchTest(),
                new SerializationTest(),
                new QueryEngineTest(),
        };
        int totalChecks = 0;
        int totalFailures = 0;
        int passedCases = 0;
        for (TestCase c : cases) {
            System.out.println("== " + c.name());
            boolean threw = false;
            try {
                c.run();
            } catch (Throwable t) {
                threw = true;
                System.out.println("    [ERROR] 测试类抛出未预期异常: " + t);
                t.printStackTrace(System.out);
            }
            totalChecks += c.checkCount();
            totalFailures += c.failureCount() + (threw ? 1 : 0);
            if (!threw && c.failureCount() == 0) {
                passedCases++;
                System.out.println("    PASS (" + c.checkCount() + " 项断言)");
            } else {
                System.out.println("    FAIL (" + c.failureCount() + "/" + c.checkCount()
                        + " 项断言失败" + (threw ? "，另有未捕获异常" : "") + ")");
            }
        }
        System.out.println();
        System.out.println("测试类: " + passedCases + "/" + cases.length + " 通过；"
                + "断言总计 " + totalChecks + " 项，失败 " + totalFailures + " 项");
        if (totalFailures != 0) {
            System.exit(1);
        }
    }
}

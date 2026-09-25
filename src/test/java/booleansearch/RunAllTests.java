package booleansearch;

import java.util.ArrayList;
import java.util.List;

/**
 * 全部自动化测试的入口：依次运行各测试类，汇总结果，全部通过退出码为 0。
 */
public final class RunAllTests {

    public static void main(String[] args) {
        List<String> failedSuites = new ArrayList<>();

        runSuite("ParserTest", failedSuites, ParserTest::run);
        runSuite("IndexTest", failedSuites, IndexTest::run);
        runSuite("JsonTest", failedSuites, JsonTest::run);
        runSuite("ExhaustiveEquivalenceTest", failedSuites, ExhaustiveEquivalenceTest::run);
        runSuite("OptimizationCostTest", failedSuites, OptimizationCostTest::run);
        runSuite("SearchServerTest", failedSuites, SearchServerTest::run);

        System.out.println();
        if (failedSuites.isEmpty()) {
            System.out.println("全部测试套件通过 ✔");
            System.exit(0);
        } else {
            System.out.println("以下测试套件存在失败: " + failedSuites);
            System.exit(1);
        }
    }

    private static void runSuite(String name, List<String> failedSuites, ThrowingRunnable suite) {
        System.out.println();
        System.out.println("########## " + name + " ##########");
        try {
            suite.run();
        } catch (Throwable t) {
            failedSuites.add(name);
            System.out.println("  [套件异常] " + t);
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }
}

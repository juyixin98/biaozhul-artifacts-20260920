package tvl.test;

import java.util.ArrayList;
import java.util.List;

/** 测试运行入口：实例化全部测试类并汇总结果。 */
public final class TestRunner {

    public static void main(String[] args) {
        List<TestBase> suites = new ArrayList<>();
        suites.add(new TruthTablesTest());
        suites.add(new ParserPrecedenceTest());
        suites.add(new ErrorPositionTest());
        suites.add(new TypeCheckerTest());
        suites.add(new EvaluatorSemanticsTest());
        suites.add(new EngineApiTest());
        suites.add(new JsonTest());

        int totalPassed = 0;
        int totalFailed = 0;
        for (TestBase suite : suites) {
            System.out.println("== " + suite.name());
            try {
                suite.run();
            } catch (Exception ex) {
                suite.fail("suite threw: " + ex);
                ex.printStackTrace(System.out);
            }
            totalPassed += suite.passed();
            totalFailed += suite.failed();
            System.out.println("   passed=" + suite.passed() + " failed=" + suite.failed());
            for (String failure : suite.failureMessages()) {
                System.out.println("   [FAIL] " + failure);
            }
        }

        System.out.println();
        System.out.println("TOTAL: " + totalPassed + " passed, " + totalFailed + " failed, "
                + (totalPassed + totalFailed) + " assertions");
        System.exit(totalFailed == 0 ? 0 : 1);
    }
}

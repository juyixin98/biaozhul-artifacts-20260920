package vecsearch.testutil;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖的极小测试框架（无 JUnit 需求，离线环境可直接用 javac+java 运行）。
 *
 * <p>用法：
 * <pre>
 *   TestRunner r = new TestRunner("Distances");
 *   r.test("l2 of known vectors", () -&gt; { ... Assert.eq(...); ... });
 *   boolean ok = r.run();
 * </pre>
 */
public final class TestRunner {

    private final String suiteName;
    private final List<Case> cases = new ArrayList<>();
    private int passed;
    private int failed;

    /** 允许测试体抛出任意异常（含受检异常），由 runner 统一捕获。 */
    @FunctionalInterface
    public interface TestBody {
        void run() throws Exception;
    }

    private record Case(String name, TestBody body) {
    }

    public TestRunner(String suiteName) {
        this.suiteName = suiteName;
    }

    public void test(String name, TestBody body) {
        cases.add(new Case(name, body));
    }

    /** 运行全部用例，打印结果，返回是否全部通过。 */
    public boolean run() {
        System.out.println("== suite: " + suiteName + " (" + cases.size() + " cases) ==");
        for (Case c : cases) {
            try {
                c.body().run();
                passed++;
                System.out.println("  PASS  " + c.name());
            } catch (AssertionError | Exception t) {
                failed++;
                System.out.println("  FAIL  " + c.name() + "  -> " + t);
            }
        }
        System.out.printf("-- %s: %d passed, %d failed%n%n", suiteName, passed, failed);
        return failed == 0;
    }

    public int failedCount() {
        return failed;
    }
}

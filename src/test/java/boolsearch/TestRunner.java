package boolsearch;

import java.util.ArrayList;
import java.util.List;

/** 测试入口：运行全部用例，输出通过/失败摘要，失败时退出码为 1。 */
public final class TestRunner {
    @FunctionalInterface
    public interface T {
        void run() throws Exception;
    }

    public record Case(String name, T t) {}

    public static void main(String[] args) {
        List<Case> cases = new ArrayList<>();
        ParserTests.register(cases);
        IndexTests.register(cases);
        ExhaustiveEvalTests.register(cases);
        OptimizeTests.register(cases);
        JsonTests.register(cases);
        ServerTests.register(cases);

        int pass = 0;
        List<String> failures = new ArrayList<>();
        for (Case c : cases) {
            try {
                c.t().run();
                pass++;
                System.out.println("PASS  " + c.name());
            } catch (Throwable e) {
                failures.add(c.name() + " -> " + e);
                System.out.println("FAIL  " + c.name() + " -> " + e);
            }
        }
        System.out.printf("%n结果: %d/%d 通过%n", pass, cases.size());
        if (!failures.isEmpty()) {
            System.out.println("未通过项:");
            for (String f : failures) System.out.println("  - " + f);
            System.exit(1);
        }
    }
}

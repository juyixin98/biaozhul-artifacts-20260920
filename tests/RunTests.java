package tests;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Supplier;

/**
 * 测试总入口：裸 JDK 运行，无需 JUnit/Maven/Gradle。
 *
 * <pre>
 *   javac -d build/classes @build/sources.txt
 *   java -cp build/classes tests.RunTests
 * </pre>
 *
 * 退出码：0=全部通过，1=有失败。
 */
public final class RunTests {

    public static void main(String[] args) {
        Map<String, Supplier<TestCase>> suite = new LinkedHashMap<>();
        suite.put("HandComputed", HandComputedTest::new);
        suite.put("Differential", DifferentialTest::new);
        suite.put("PolicyAndLateness", PolicyAndLatenessTest::new);
        suite.put("ProcessingTime", ProcessingTimeTest::new);
        suite.put("Json", JsonTest::new);
        suite.put("Service", ServiceTest::new);
        suite.put("HttpServer", HttpServerTest::new);

        int totalChecks = 0;
        int failedSuites = 0;
        for (Map.Entry<String, Supplier<TestCase>> entry : suite.entrySet()) {
            String name = entry.getKey();
            TestCase tc = entry.getValue().get();
            java.util.List<String> failures;
            try {
                tc.run();
                failures = tc.failures();
            } catch (Throwable t) {
                System.out.printf("[FAIL] %-20s 测试抛出异常: %s%n", name, t);
                t.printStackTrace(System.out);
                failedSuites++;
                continue;
            }
            totalChecks += tc.checkCount();
            if (failures.isEmpty()) {
                System.out.printf("[PASS] %-20s  (%d 项断言)%n", name, tc.checkCount());
            } else {
                failedSuites++;
                System.out.printf("[FAIL] %-20s  %d/%d 项断言失败%n",
                        name, failures.size(), tc.checkCount());
                for (String f : failures) {
                    System.out.println("        - " + f);
                }
            }
        }
        System.out.println("--------------------------------------------------");
        if (failedSuites == 0) {
            System.out.println("全部测试通过：" + suite.size() + " 个测试类，" + totalChecks + " 项断言");
            System.exit(0);
        } else {
            System.out.println(failedSuites + " 个测试类存在失败，共 " + totalChecks + " 项断言");
            System.exit(1);
        }
    }
}

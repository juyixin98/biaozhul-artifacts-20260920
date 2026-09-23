package cdcrebuild.test;

import java.util.ArrayList;
import java.util.List;

/** 测试总入口：运行全部套件，任意失败则以退出码 1 结束（便于 CI）。 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        List<TestRunner> suites = new ArrayList<>();

        TestRunner core = new TestRunner("核心引擎");
        CoreEngineTest.run(core);
        suites.add(core);

        TestRunner restart = new TestRunner("重启 / 半事务 / WAL");
        RestartTest.run(restart);
        suites.add(restart);

        TestRunner random = new TestRunner("随机对账");
        RandomCompareTest.run(random);
        suites.add(random);

        TestRunner http = new TestRunner("HTTP 端到端");
        HttpTest.run(http);
        suites.add(http);

        int total = 0;
        int failed = 0;
        System.out.println();
        System.out.println("================ 测试汇总 ================");
        for (TestRunner s : suites) {
            total += s.count();
            failed += s.failureCount();
            System.out.printf("  %-22s %3d 项，失败 %d%n",
                    s.suiteName(), s.count(), s.failureCount());
        }
        System.out.println("------------------------------------------");
        System.out.printf("  合计 %d 项，失败 %d%n", total, failed);
        System.out.println("==========================================");
        System.exit(failed == 0 ? 0 : 1);
    }
}

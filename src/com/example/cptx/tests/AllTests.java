package com.example.cptx.tests;

import java.util.ArrayList;
import java.util.List;

/** 测试总入口：运行全部套件，汇总退出码（有失败则非 0，便于 CI/脚本判断）。 */
public final class AllTests {

    private AllTests() {}

    public static void main(String[] args) throws Exception {
        TestDirs.deleteAll();

        List<TestRunner> runners = new ArrayList<>();

        TestRunner core = new TestRunner("CoreTest");
        runners.add(core);
        CoreTest.run(core);
        core.finish();

        TestRunner recovery = new TestRunner("RecoveryTest");
        runners.add(recovery);
        RecoveryTest.run(recovery);
        recovery.finish();

        TestRunner service = new TestRunner("ServiceTest");
        runners.add(service);
        ServiceTest.run(service);
        service.finish();

        int totalChecks = runners.stream().mapToInt(TestRunner::checks).sum();
        int totalFailures = runners.stream().mapToInt(r -> r.failures().size()).sum();

        System.out.println();
        System.out.println("==================================================");
        System.out.println("总计: " + totalChecks + " 项断言，" + totalFailures + " 项失败");
        if (totalFailures > 0) {
            System.exit(1);
        }
    }
}

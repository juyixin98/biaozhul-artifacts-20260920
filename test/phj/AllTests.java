package phj;

import phj.cli.CliEndToEndTest;
import phj.join.CorrectnessFuzzTest;
import phj.join.DiskQuotaTest;
import phj.join.EngineSemanticsTest;
import phj.join.SpillMechanicsTest;
import phj.json.JsonTest;
import phj.server.HttpServerTest;

/** 全部自动化测试的统一入口。 */
public final class AllTests {

    private AllTests() {}

    public static void main(String[] args) {
        TestRunner runner = new TestRunner();
        runner.run(JsonTest.class);
        runner.run(EngineSemanticsTest.class);
        runner.run(SpillMechanicsTest.class);
        runner.run(CorrectnessFuzzTest.class);
        runner.run(DiskQuotaTest.class);
        runner.run(CliEndToEndTest.class);
        runner.run(HttpServerTest.class);
        System.exit(runner.summary());
    }
}

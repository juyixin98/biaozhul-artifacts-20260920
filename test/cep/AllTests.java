package cep;

/**
 * 自动化测试入口。运行：
 *   java -cp build/classes:build/test-classes cep.AllTests
 * 退出码 0 表示全部通过，非 0 表示有失败（供 CI / run.sh 使用）。
 */
public final class AllTests {

    private AllTests() {
    }

    public static void main(String[] args) {
        int code = TestRunner.run(
                EngineTest.class,
                EventLogTest.class,
                RecoveryTest.class,
                HttpTest.class,
                RecoveryProcessTest.class);
        System.exit(code);
    }
}

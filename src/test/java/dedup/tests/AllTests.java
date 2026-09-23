package dedup.tests;

/** 全部测试入口：java -cp build/classes:build/test-classes dedup.tests.AllTests */
public final class AllTests {

    private AllTests() {}

    public static void main(String[] args) {
        System.out.println("=== 乱序去重与墓碑：自动化测试 ===");
        System.out.println();
        var tf = new TestFramework();

        System.out.println("[单元] JSON 工具");
        JsonTest.register(tf);
        System.out.println("[单元] 去重算子边界");
        DeduplicatorEdgeTest.register(tf);
        System.out.println("[验收1] 同 ID 不同载荷");
        SameIdDifferentPayloadTest.register(tf);
        System.out.println("[验收2] 时钟回退");
        ClockFallbackTest.register(tf);
        System.out.println("[验收3] 重启恢复");
        RestartRecoveryTest.register(tf);
        System.out.println("[验收4] 极迟重复");
        VeryLateDuplicateTest.register(tf);
        System.out.println("[强度] 差分测试（参考实现 vs 有界算子）");
        DifferentialTest.register(tf);
        System.out.println("[端到端] HTTP 服务");
        HttpEndToEndTest.register(tf);

        int code = tf.summary();
        System.exit(code);
    }
}

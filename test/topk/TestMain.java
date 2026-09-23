package topk;

/** 测试入口：运行全部测试，任一失败即非零退出。 */
public final class TestMain {
    public static void main(String[] args) throws Exception {
        TopKServiceTest.runAll();
        HttpApiTest.runAll();
        System.out.println("ALL TESTS PASSED");
    }
}

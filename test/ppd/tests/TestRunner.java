package ppd.tests;

/** 自动化测试入口：运行全部测试，有失败则以非零码退出。 */
public class TestRunner {

    public static void main(String[] args) {
        Assert a = new Assert();
        ExprTest.run(a);
        ExecutorTest.run(a);
        ExhaustiveTest.run(a);
        a.summary();
        System.exit(a.failed() == 0 ? 0 : 1);
    }
}

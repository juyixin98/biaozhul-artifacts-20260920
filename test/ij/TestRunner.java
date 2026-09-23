package ij;

/** 测试运行入口：依次执行全部测试类，任一用例失败则退出码非零。 */
public final class TestRunner {

    public static void main(String[] args) {
        Assert asserts = new Assert();
        String[] names = {
                "JsonTest",
                "JoinSessionTest",
                "AcceptanceTest",
                "ApiServerTest",
        };
        for (String name : names) {
            System.out.println("== " + name);
            long t0 = System.nanoTime();
            try {
                var ctor = Class.forName("ij." + name).getDeclaredConstructor(Assert.class);
            ctor.setAccessible(true);
            ctor.newInstance(asserts);
            } catch (Exception e) {
                Throwable cause = e.getCause() != null ? e.getCause() : e;
                cause.printStackTrace();
                asserts.fail(name + " threw: " + cause);
            }
            System.out.printf("   %.1f ms%n", (System.nanoTime() - t0) / 1e6);
        }

        System.out.println();
        System.out.println("checks: " + asserts.checks() + ", failures: " + asserts.failures());
        if (asserts.failures() > 0) {
            System.out.println("RESULT: FAIL");
            System.exit(1);
        }
        System.out.println("RESULT: PASS");
    }

    private TestRunner() {
    }
}

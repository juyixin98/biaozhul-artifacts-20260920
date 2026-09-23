package test;

/**
 * 全部测试的统一入口（零依赖）：依次运行四个测试套件，
 * 任一套件失败则以退出码 1 结束，便于 CI / 脚本判定。
 */
public final class RunAllTests {

    public static void main(String[] args) {
        int failures = 0;
        failures += run("test.JsonTest");
        failures += run("test.WindowEngineTest");
        failures += run("test.DifferentialTest");
        failures += run("test.ExecutorTest");

        System.out.println();
        if (failures == 0) {
            System.out.println("全部测试套件通过。");
        } else {
            System.out.println(failures + " 个测试套件失败。");
            System.exit(1);
        }
    }

    private static int run(String className) {
        try {
            Class<?> c = Class.forName(className);
            var main = c.getMethod("main", String[].class);
            Object ok = main.invoke(null, (Object) new String[0]);
            return Boolean.TRUE.equals(ok) ? 0 : 1;
        } catch (java.lang.reflect.InvocationTargetException e) {
            System.out.println("[ERROR] " + className + " 抛出未预期异常："
                    + e.getTargetException());
            e.getTargetException().printStackTrace(System.out);
            return 1;
        } catch (Exception e) {
            System.out.println("[ERROR] 无法运行 " + className + "：" + e);
            return 1;
        }
    }
}

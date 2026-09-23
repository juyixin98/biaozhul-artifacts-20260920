package windowengine;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * 迷你测试运行器：扫描指定测试类中所有名为 test* 的无参方法（含 @Test 标记或 test 前缀），
 * 逐个执行，统计通过/失败。main 聚合多个测试类，有失败时以退出码 1 结束。
 */
public final class TestRunner {

    private TestRunner() {
    }

    public static void main(String[] args) throws Exception {
        List<Class<?>> classes = new ArrayList<>();
        for (String name : args) {
            classes.add(Class.forName(name));
        }
        int passed = 0;
        int failed = 0;
        List<String> failures = new ArrayList<>();

        for (Class<?> clazz : classes) {
            Object instance = clazz.getDeclaredConstructor().newInstance();
            for (Method method : clazz.getDeclaredMethods()) {
                if (!method.getName().startsWith("test")) {
                    continue;
                }
                method.setAccessible(true);
                String label = clazz.getSimpleName() + "." + method.getName();
                try {
                    method.invoke(instance);
                    passed++;
                    System.out.println("PASS " + label);
                } catch (Exception e) {
                    failed++;
                    Throwable cause = e.getCause() != null ? e.getCause() : e;
                    failures.add(label + " :: " + cause);
                    System.out.println("FAIL " + label + " :: " + cause);
                }
            }
        }

        System.out.println();
        System.out.println("================================================");
        System.out.println("测试结果: " + passed + " 通过, " + failed + " 失败，共 "
                + (passed + failed) + " 个");
        if (failed > 0) {
            System.out.println("------------------------------------------------");
            for (String f : failures) {
                System.out.println("  ✗ " + f);
            }
            System.exit(1);
        }
    }
}

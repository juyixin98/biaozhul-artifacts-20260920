package com.tjoin;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖测试运行器：反射执行所有注册类中带 {@link Test} 注解的方法。
 *
 * <p>输出每个方法的通过/失败及异常摘要；全部通过退出码 0，否则 1。
 */
public final class TestRunner {

    private TestRunner() {
    }

    public static void main(String[] args) {
        List<Class<?>> suites = new ArrayList<>();
        suites.add(com.tjoin.core.IntervalJoinOperatorTest.class);
        suites.add(com.tjoin.core.ReferenceConsistencyTest.class);
        suites.add(com.tjoin.time.TimeServiceTest.class);
        suites.add(com.tjoin.service.JsonTest.class);
        suites.add(com.tjoin.service.JoinSimulationTest.class);
        suites.add(com.tjoin.service.JoinHttpServerTest.class);

        int run = 0;
        int passed = 0;
        final List<String> failures = new ArrayList<>();

        for (Class<?> suite : suites) {
            for (Method m : suite.getDeclaredMethods()) {
                if (!m.isAnnotationPresent(Test.class)) {
                    continue;
                }
                run++;
                String label = suite.getSimpleName() + "." + m.getName();
                try {
                    Object instance = suite.getDeclaredConstructor().newInstance();
                    m.setAccessible(true);
                    m.invoke(instance);
                    passed++;
                    System.out.println("PASS " + label);
                } catch (java.lang.reflect.InvocationTargetException ite) {
                    Throwable cause = ite.getCause();
                    failures.add(label + " -> " + cause);
                    System.out.println("FAIL " + label + " -> " + cause);
                    cause.printStackTrace(System.out);
                } catch (Exception e) {
                    failures.add(label + " -> " + e);
                    System.out.println("FAIL " + label + " -> " + e);
                    e.printStackTrace(System.out);
                }
            }
        }

        System.out.println();
        System.out.println("--------------------------------------------------");
        System.out.println("Tests run: " + run + ", passed: " + passed
                + ", failed: " + (run - passed));
        if (!failures.isEmpty()) {
            System.out.println("Failed tests:");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            System.exit(1);
        }
    }
}

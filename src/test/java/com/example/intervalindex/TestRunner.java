package com.example.intervalindex;

import java.lang.reflect.Method;

/**
 * 零依赖测试运行器：反射调用各测试类的静态 {@code run()}。
 *
 * <pre>javac -d build/test ... &amp;&amp; java -cp build/classes:build/test com.example.intervalindex.TestRunner</pre>
 */
public final class TestRunner {

    private static final String[] SUITES = {
            "com.example.intervalindex.core.IntervalIndexTest",
            "com.example.intervalindex.json.JsonTest",
            "com.example.intervalindex.http.HttpApiTest",
    };

    public static void main(String[] args) {
        int passed = 0;
        int failed = 0;
        for (String name : SUITES) {
            try {
                Class<?> cls = Class.forName(name);
                Method run = cls.getDeclaredMethod("run");
                run.setAccessible(true);
                System.out.println("== running " + name);
                run.invoke(null);
                passed++;
            } catch (Exception e) {
                failed++;
                Throwable cause = e.getCause() != null ? e.getCause() : e;
                System.out.println("FAILED " + name + ": " + cause);
                cause.printStackTrace(System.out);
            }
        }
        System.out.println();
        System.out.println("suites: " + (passed + failed) + ", passed: " + passed
                + (failed == 0 ? "" : ", FAILED: " + failed));
        if (failed != 0) {
            System.exit(1);
        }
    }
}

package com.tvl.test;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 测试运行器：注册多个测试套件（名字 + 断言回调），统一执行、汇总、退出码。
 */
public final class TestRunner {

    @FunctionalInterface
    public interface Suite {
        void run(Assertions a) throws Exception;
    }

    private final Map<String, Suite> suites = new LinkedHashMap<>();

    public TestRunner add(String name, Suite suite) {
        suites.put(name, suite);
        return this;
    }

    public int run() {
        int totalChecks = 0;
        int failedSuites = 0;
        System.out.println("== TVL query executor test suite ==");
        for (Map.Entry<String, Suite> e : suites.entrySet()) {
            Assertions a = new Assertions();
            String status;
            try {
                e.getValue().run(a);
            } catch (RuntimeException | Error boom) {
                a.fail("套件抛出未预期异常: "
                        + boom.getClass().getSimpleName() + ": " + boom.getMessage());
                StackTraceElement top = boom.getStackTrace().length > 0
                        ? boom.getStackTrace()[0] : null;
                if (top != null) {
                    a.fail("    at " + top);
                }
            } catch (Exception checked) {
                a.fail("套件抛出受检异常: " + checked);
            }
            totalChecks += a.checkCount();
            if (a.failures().isEmpty()) {
                status = "PASS (" + a.checkCount() + " 项断言)";
            } else {
                status = "FAIL (" + a.failures().size() + "/" + a.checkCount() + " 项断言失败)";
                failedSuites++;
            }
            System.out.printf("  [%s] %s%n", a.failures().isEmpty() ? "OK  " : "FAIL",
                    e.getKey() + " — " + status);
            for (String f : a.failures()) {
                System.out.println("      ✗ " + f);
            }
        }
        System.out.printf("== 共 %d 个套件，%d 项断言，%d 个套件失败 ==%n",
                suites.size(), totalChecks, failedSuites);
        return failedSuites == 0 ? 0 : 1;
    }
}

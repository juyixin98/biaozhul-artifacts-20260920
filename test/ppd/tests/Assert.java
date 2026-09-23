package ppd.tests;

import java.util.ArrayList;
import java.util.List;

/** 零依赖的极简断言框架。 */
public class Assert {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public void check(String name, boolean condition) {
        if (condition) {
            passed++;
        } else {
            failed++;
            failures.add(name);
            System.out.println("  [FAIL] " + name);
        }
    }

    public void fail(String name, String detail) {
        failed++;
        failures.add(name + " -> " + detail);
        System.out.println("  [FAIL] " + name + " :: " + detail);
    }

    public void section(String title) {
        System.out.println();
        System.out.println("== " + title + " ==");
    }

    public int passed() { return passed; }

    public int failed() { return failed; }

    public List<String> failures() { return failures; }

    public void summary() {
        System.out.println();
        System.out.println("----------------------------------------");
        System.out.println("通过: " + passed + "，失败: " + failed);
        if (failed > 0) {
            System.out.println("失败用例:");
            for (String f : failures) System.out.println("  - " + f);
        }
    }
}

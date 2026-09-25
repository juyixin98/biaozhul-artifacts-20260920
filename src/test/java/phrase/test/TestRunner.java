package phrase.test;

import java.util.LinkedHashMap;
import java.util.Map;

/** 测试注册表与运行器：零依赖，不使用 JUnit。 */
public final class TestRunner {

    private final Map<String, TestCase> tests = new LinkedHashMap<>();
    private int passed;
    private int failed;

    public void add(String name, TestCase tc) {
        if (tests.put(name, tc) != null) {
            throw new IllegalStateException("duplicate test name: " + name);
        }
    }

    public int run() {
        System.out.println("Running " + tests.size() + " tests...");
        for (Map.Entry<String, TestCase> e : tests.entrySet()) {
            String name = e.getKey();
            try {
                e.getValue().run();
                passed++;
                System.out.println("  PASS  " + name);
            } catch (Throwable t) {
                failed++;
                System.out.println("  FAIL  " + name + "  -> " + t);
            }
        }
        System.out.println();
        System.out.println("Tests run: " + tests.size() + ", passed: " + passed
                + ", failed: " + failed);
        return failed == 0 ? 0 : 1;
    }
}

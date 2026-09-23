package topk;

import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖轻量测试框架：注册用例、运行、统计、失败时进程退出码非零。
 */
final class TestHarness {

    @FunctionalInterface
    interface TestCase {
        void run() throws Exception;
    }

    private final String suiteName;
    private final List<Named> cases = new ArrayList<>();
    private int passed;
    private int failed;

    private static final class Named {
        final String name;
        final TestCase body;

        Named(String name, TestCase body) {
            this.name = name;
            this.body = body;
        }
    }

    TestHarness(String suiteName) {
        this.suiteName = suiteName;
    }

    void add(String name, TestCase body) {
        cases.add(new Named(name, body));
    }

    int run() {
        System.out.println("==== " + suiteName + "：" + cases.size() + " 个用例 ====");
        for (Named c : cases) {
            try {
                c.body.run();
                passed++;
                System.out.println("[PASS] " + c.name);
            } catch (AssertionError | Exception t) {
                failed++;
                System.out.println("[FAIL] " + c.name + " -> " + t);
            }
        }
        System.out.println("---- " + suiteName + " 结果: " + passed + " 通过, "
                + failed + " 失败, 共 " + cases.size() + " ----");
        return failed == 0 ? 0 : 1;
    }

    // ---------------- 断言 ----------------

    static void check(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    static void eq(Object actual, Object expected, String msg) {
        boolean ok;
        if (actual instanceof Number && expected instanceof Number) {
            // JSON 数字可能是 Long/Integer/Double，按数值比较
            ok = ((Number) actual).doubleValue() == ((Number) expected).doubleValue();
        } else {
            ok = (actual == null) ? expected == null : actual.equals(expected);
        }
        if (!ok) {
            throw new AssertionError(msg + " | 期望=" + expected + " 实际=" + actual);
        }
    }

    static void eqRows(List<GroupState.Row> actual, String expected, String msg) {
        StringBuilder sb = new StringBuilder();
        sb.append('[');
        for (int i = 0; i < actual.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            GroupState.Row r = actual.get(i);
            sb.append(r.itemId).append('=').append(r.score);
        }
        sb.append(']');
        String actualStr = sb.toString();
        eq(actualStr, expected, msg);
    }
}

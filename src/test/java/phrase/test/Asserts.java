package phrase.test;

import java.util.Arrays;
import java.util.List;
import java.util.Objects;

/** 极简断言工具，失败时抛出带消息的 AssertionError。 */
public final class Asserts {

    private Asserts() {}

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    public static void assertFalse(boolean cond, String msg) {
        assertTrue(!cond, msg);
    }

    public static void assertEquals(Object expected, Object actual, String msg) {
        if (!Objects.equals(expected, actual)) {
            throw new AssertionError(msg + " — expected:<" + expected + "> but was:<" + actual + ">");
        }
    }

    public static void assertEquals(int expected, int actual, String msg) {
        if (expected != actual) {
            throw new AssertionError(msg + " — expected:<" + expected + "> but was:<" + actual + ">");
        }
    }

    public static void assertEquals(List<Integer> expected, int[] actual, String msg) {
        assertEquals(expected, Arrays.stream(actual).boxed().toList(), msg);
    }

    public static void assertContains(List<int[]> tuples, int[] target, String msg) {
        for (int[] t : tuples) {
            if (Arrays.equals(t, target)) {
                return;
            }
        }
        throw new AssertionError(msg + " — tuple " + Arrays.toString(target)
                + " not found in " + tuples.stream().map(Arrays::toString).toList());
    }

    public static void assertNotContains(List<int[]> tuples, int[] target, String msg) {
        for (int[] t : tuples) {
            if (Arrays.equals(t, target)) {
                throw new AssertionError(msg + " — tuple " + Arrays.toString(target)
                        + " unexpectedly found");
            }
        }
    }

    public static void fail(String msg) {
        throw new AssertionError(msg);
    }

    /** 断言抛出指定类型异常。 */
    public static void assertThrows(Class<? extends Throwable> expected, RunnableEx r, String msg) {
        try {
            r.run();
        } catch (Throwable t) {
            if (expected.isInstance(t)) {
                return;
            }
            throw new AssertionError(msg + " — expected exception " + expected.getSimpleName()
                    + " but got " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError(msg + " — expected exception " + expected.getSimpleName()
                + " but nothing was thrown");
    }

    @FunctionalInterface
    public interface RunnableEx {
        void run() throws Exception;
    }
}

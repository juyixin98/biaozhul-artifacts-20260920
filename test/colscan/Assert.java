package colscan;

/** Tiny assertion harness — no external test framework required. */
public final class Assert {
    private int checks;

    public void check(boolean cond, String message) {
        checks++;
        if (!cond) throw new AssertionError(message);
    }

    public void eq(Object actual, Object expected, String message) {
        check(java.util.Objects.equals(actual, expected),
                message + " — expected <" + expected + "> but was <" + actual + ">");
    }

    public void isNull(Object actual, String message) {
        check(actual == null, message + " — expected null but was <" + actual + ">");
    }

    public int checkCount() {
        return checks;
    }
}

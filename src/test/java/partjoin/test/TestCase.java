package partjoin.test;

import java.util.ArrayList;
import java.util.List;

/** Minimal dependency-free test harness: name + body, assert* record failures. */
public abstract class TestCase {

    public final String name;
    private final List<String> failures = new ArrayList<>();
    private Throwable unexpected;

    protected TestCase(String name) {
        this.name = name;
    }

    protected abstract void run() throws Exception;

    public final boolean execute() {
        try {
            run();
        } catch (AssertionError e) {
            failures.add(e.getMessage());
        } catch (Throwable t) {
            unexpected = t;
        }
        return passed();
    }

    public boolean passed() {
        return failures.isEmpty() && unexpected == null;
    }

    public List<String> failures() {
        return failures;
    }

    public Throwable unexpected() {
        return unexpected;
    }

    protected void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    protected void eq(long actual, long expected, String msg) {
        if (actual != expected) {
            throw new AssertionError(msg + " — expected " + expected + " but got " + actual);
        }
    }

    protected void eq(Object actual, Object expected, String msg) {
        if (actual == null ? expected != null : !actual.equals(expected)) {
            throw new AssertionError(msg + " — expected <" + expected + "> but got <" + actual + ">");
        }
    }
}

package streamagg.test;

/** Thrown when an assertion in the tiny test framework fails. */
public final class TestFailure extends AssertionError {
    public TestFailure(String message) {
        super(message);
    }
}

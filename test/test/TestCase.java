package test;

/** One named test case. */
@FunctionalInterface
public interface TestCase {
    void run() throws Exception;
}

package dev.example.cp.tests;

/** 测试断言失败。 */
public class AssertionFailure extends RuntimeException {
    public AssertionFailure(String message) {
        super(message);
    }
}

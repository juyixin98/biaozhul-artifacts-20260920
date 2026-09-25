package sessions.testing;

/** 测试断言失败。 */
public class AssertionFailure extends AssertionError {
    public AssertionFailure(String message) {
        super(message);
    }
}

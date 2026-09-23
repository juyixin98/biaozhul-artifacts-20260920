package joinorder;

/** 用户请求非法或运行期可预期错误，错误信息可直接展示给调用方。 */
public class EngineException extends RuntimeException {
    public EngineException(String message) {
        super(message);
    }
}
